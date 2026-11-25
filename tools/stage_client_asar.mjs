// Build a new server.asar: every entry of the original archive in its original order, except the
// folder apps/server/dist/client, which comes from a freshly built client folder.
//
// node stage_client_asar.mjs <@electron/asar package dir> <original server.asar> <client dir> <out server.asar> [unpacked dir]
//
// - Packed files are read from the original archive by byte range, unpacked files from
//   <original>.unpacked, so their bytes cannot change. Integrity hashes are recomputed from them.
// - Each entry keeps its "unpacked" and "executable" flags from the original header.
// - T3 Code packs server.asar on Linux (release.yml: Windows targets build on Linux), where the
//   packer sets "executable" from the file mode. On Windows it never does, so this script reports
//   process.platform as "linux" to the packer, and passes each mode from the original header.
// - Run it from an empty folder: for small files the packer first tries fs.readFileSync(<path in the
//   archive>) relative to the working folder, and only falls back to the stream when that fails.
//
// Uses @electron/asar 4.3.0 (MIT), the version T3 Code's own release packs server.asar with.
import fs from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { pathToFileURL } from "node:url";

const [moduleDir, original, clientDir, out, unpackedArg] = process.argv.slice(2);
if (!out) {
  console.error("usage: node stage_client_asar.mjs <asar module dir> <original server.asar> <client dir> <out server.asar> [unpacked dir]");
  process.exit(2);
}
if (fs.readdirSync(process.cwd()).length > 0) {
  console.error("run this from an empty folder (see the header comment)");
  process.exit(2);
}
const asar = await import(pathToFileURL(path.join(moduleDir, "lib", "asar.js")).href);
const CLIENT = "apps/server/dist/client";

const fd = fs.openSync(original, "r");
const head = Buffer.alloc(16);
fs.readSync(fd, head, 0, 16, 0);
fs.closeSync(fd);
const base = 8 + head.readUInt32LE(4);
const header = JSON.parse(asar.getRawHeader(original).headerString);
// The installed <server.asar>.unpacked by default. A backup copy of the archive has none beside it.
const unpackedRoot = unpackedArg || `${original}.unpacked`;

const streams = [];
const empty = () => Readable.from([]);
const modeOf = (node) => (node.executable ? 0o100755 : 0o100644);

function addOriginal(node, prefix) {
  for (const [name, child] of Object.entries(node.files)) {
    const p = prefix ? `${prefix}/${name}` : name;
    if (p === CLIENT) {
      addClient(clientDir, CLIENT);
      continue;
    }
    if (child.files) {
      streams.push({ type: "directory", path: p, unpacked: Boolean(child.unpacked) });
      addOriginal(child, p);
    } else if (child.link !== undefined) {
      throw new Error(`links are not handled: ${p}`);
    } else {
      const size = Number(child.size);
      const stat = { mode: modeOf(child), size };
      let streamGenerator;
      if (size === 0) {
        streamGenerator = empty;
      } else if (child.unpacked) {
        const src = path.join(unpackedRoot, ...p.split("/"));
        streamGenerator = () => fs.createReadStream(src);
      } else {
        const start = base + Number(child.offset);
        streamGenerator = () => fs.createReadStream(original, { start, end: start + size - 1 });
      }
      streams.push({ type: "file", path: p, unpacked: Boolean(child.unpacked), stat, streamGenerator });
    }
  }
}

function addClient(dir, prefix) {
  streams.push({ type: "directory", path: prefix, unpacked: false });
  for (const name of fs.readdirSync(dir).sort()) {
    const full = path.join(dir, name);
    const p = `${prefix}/${name}`;
    const st = fs.statSync(full);
    if (st.isDirectory()) {
      addClient(full, p);
    } else {
      const size = st.size;
      streams.push({
        type: "file", path: p, unpacked: false, stat: { mode: 0o100644, size },
        streamGenerator: size === 0 ? empty : () => fs.createReadStream(full),
      });
    }
  }
}

addOriginal(header, "");
if (!streams.some((s) => s.path === CLIENT)) throw new Error(`${CLIENT} is not in the original archive`);
Object.defineProperty(process, "platform", { value: "linux" });
await asar.createPackageFromStreams(out, streams);
console.log(`packed ${streams.filter((s) => s.type === "file").length} files into ${out} (${fs.statSync(out).size} bytes)`);

// The release signs the unpacked native files after it packs (build-desktop-artifact.ts, "Smart App
// Control validates unpacked native libraries independently; sign them too"). So the original
// header holds each unpacked file's hash from before signing, and the packer above hashed the
// signed file. Put the original integrity records back. The header text keeps its byte length, so
// it is rewritten in place and no file data moves.
const raw = asar.getRawHeader(out).headerString;
const built = JSON.parse(raw);
if (JSON.stringify(built) !== raw) throw new Error("the new header does not round-trip through JSON");
const nodeAt = (root, p) => p.split("/").reduce((n, part) => n.files[part], root);
let restored = 0;
(function visit(node, prefix) {
  for (const [name, child] of Object.entries(node.files)) {
    const p = prefix ? `${prefix}/${name}` : name;
    if (child.files) visit(child, p);
    else if (child.unpacked) {
      const was = nodeAt(header, p);
      if (Number(was.size) !== Number(child.size)) throw new Error(`size differs: ${p}`);
      child.integrity = was.integrity;
      restored += 1;
    }
  }
})(built, "");
const text = JSON.stringify(built);
if (Buffer.byteLength(text) !== Buffer.byteLength(raw)) throw new Error("the header length would change");
const fdw = fs.openSync(out, "r+");
const check = Buffer.alloc(16);
fs.readSync(fdw, check, 0, 16, 0);
if (check.readUInt32LE(12) !== Buffer.byteLength(raw)) throw new Error("unexpected header layout");
fs.writeSync(fdw, Buffer.from(text), 0, Buffer.byteLength(text), 16);
fs.closeSync(fdw);
if (asar.getRawHeader(out).headerString !== text) throw new Error("header rewrite did not read back");
console.log(`kept the original integrity records of ${restored} unpacked files`);
