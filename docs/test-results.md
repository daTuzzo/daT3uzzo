# Read-aloud test: OpenAI Realtime, 23 Nov 2026

Goal: check that a Realtime model can read T3 Code replies aloud, clean them up for listening, and keep every fact.

## Setup

- 5 replies of the kind T3 Code writes: findings with a table of links, a design with nested lists, a report with a table and addresses, numbered steps, and a CI fix with code blocks and money. The replies used in the test are not in this repo. `eval/samples/` holds made-up replies that test the same things.
- The test scripts are not in this repo. Their logic lives on here: the code cleanup is `dat3uzzo/cleanup.py`, the number spelling is `dat3uzzo/spoken_numbers.py`, and the instructions are in `dat3uzzo/realtime.py`.
- Each reply ran through one Realtime session, piece by piece, with voice `marin` and PCM 24 kHz.
- Round 2 only: code spelled numbers, dates, money, versions, IPs and IDs before the model saw the text.
- Speech-to-text (`gpt-4o-mini-transcribe`, cross-checked with `gpt-4o-transcribe` and `whisper-1`) turned each audio file back into text. A script then listed every number difference between the source and the audio.
- The key came from T3 Code's own dictation key file. No script printed it. A search of the outputs for key text found 0 files.

## Results

| Setup | Confirmed errors in the audio |
|---|---|
| mini, read exactly, round 1 | Digit misreads in 3 of 5 replies: a five-digit number with one wrong digit, a three-digit number off by nine, a six-digit number with a digit dropped. Two speech-to-text models agree. |
| mini, cleanup on, round 1 | 1 of 5: table rows repeated with another row's values, 2 rows dropped. Three speech-to-text models agree. |
| full, read exactly, round 1 | None found. |
| full, cleanup on, round 1 | 1 of 5: one group of an IP address read as the wrong number ("one hundred" became "two hundred"). |
| mini, numbers spelled in code, round 2 | Still repeats and drops table rows in reply 1, both modes. Code-block labels skipped in reply 5. |
| full, numbers spelled in code, cleanup on, round 2 | None found in 5 replies. |

Other findings:

- The model's own transcript is not a record of the audio. In the mini round-1 case, the transcript was correct and the audio was wrong. A drift check must use speech-to-text on the audio.
- Full model, round 2: first audio after a median of 1.04 s (max 2.37 s). It generates 3.82 s of audio per second. Cost is $0.0888 per audio minute, about $5.33 per hour of listening.
- Total test spend: $4.447 on Realtime, plus a small speech-to-text cost that was not priced.

## Decision

- Use `gpt-realtime-2.1`, not the mini model.
- Code spells numbers, dates, money, versions and IDs. The model only smooths wording.
- Keep the "Read exactly / Adapt for listening" switch. Both modes use the same code cleanup.
