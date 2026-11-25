All four backup jobs ran last night, and three of them finished. The one that failed ran out of disk space, and its data is safe on the source machine.

**What I checked** (read-only, on each machine): the job logs, the size of each archive, the checksum file next to it, the free space on the target disk, and the last restore test.

| Machine | Result |
|---|---|
| `alpha` | Finished. 18.4 GB in 22 min, checksum OK. |
| `beta` | Finished. 2.1 GB in 3 min 40 s, checksum OK. |
| `gamma` | Failed at 61%. The target disk was full. |
| `delta` | Finished. 640 MB, checksum OK. The last restore test is 41 days old. |

**gamma in detail**
- The job started at 02:00 and stopped at 02:37 with `ENOSPC`.
- The target disk has 931 GB, and 927 GB were in use. The oldest 12 archives take 310 GB.
- One address, 198.51.100.24, wrote 1,482 files to the share on 21 Nov. That is the build cache of the test runner, and it belongs in `/scratch`, not in the backup share.
- The retention rule keeps 30 daily archives, but the cleanup script has not run since 9 Nov. Its log ends with "permission denied".

**All four machines**
- Every job connected from 192.0.2.10, the backup server, with the key `backup-ed25519`.
- No job took longer than its 2-hour limit.

**What I could not cover**
- `epsilon`: the connection to 203.0.113.7 timed out. It may be off.
- A checksum shows that the archive matches what the job read. It does not show that a restore works. Only a restore test does.

The checks are in [check-backups.sh](C:\work\backup-check\check-backups.sh), and the output for each machine is in [out](C:\work\backup-check\out). To run it again on one machine: `ssh gamma 'bash -s' < check-backups.sh`.
