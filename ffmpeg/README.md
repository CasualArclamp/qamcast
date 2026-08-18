# Put ffmpeg here

Drop `ffmpeg.exe` (and `ffprobe.exe`) into this folder, or unpack a build so
that they land in `ffmpeg/bin/`. Either layout is found.

Nothing in here is committed — only this file is.

## Which build

Any recent ffmpeg decodes everything QAMcast can receive, and encodes Opus,
which is the default and the recommendation.

**HE-AAC encoding needs `libfdk_aac`**, which is not in most packaged builds
because it cannot be redistributed under the GPL — you want one built
`--enable-nonfree`. QAMcast asks your ffmpeg what it can really do, by encoding
a twentieth of a second with each codec, and greys out anything it cannot,
so you will be told rather than left guessing.

xHE-AAC does not come from ffmpeg at all; see `tools/build_exhale.py`.

## Where else it looks

In order, first hit wins:

1. `--ffmpeg` on the command line
2. `QAMCAST_FFMPEG` in the environment
3. `"ffmpeg"` in `qamcast.local.json`, beside the project
4. this folder, then `ffmpeg/bin/`, then `bin/`
5. `ffmpeg` on `PATH`

`qamcast.local.json` is the one to use if your ffmpeg lives somewhere else on
this machine and you would rather not move it. It is gitignored, so it stays
on your computer:

```json
{
  "ffmpeg": "C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe",
  "stations": "D:\\Radio Stream"
}
```
