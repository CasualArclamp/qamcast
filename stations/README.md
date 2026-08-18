# Put your radio stream playlists here

`.pls` and `.m3u` files dropped in this folder show up in the transmit page's
**Station** dropdown, and in `python tx.py --list-stations`.

Nothing in here is committed — only this file is.

You never *need* them: any URL or file ffmpeg can open still works by typing it
into the Source box. This is only the shortcut list, so that the feeds you use
often are one click rather than a paste.

## What a playlist gives you

Most Icecast setups, SomaFM among them, put the rate and codec in the path —
`groovesalad-128-aac`. QAMcast reads that, so one station with several feeds
comes out as one entry with a rate to choose from, and the transmit page can
tell you what each feed will cost before you pick it. A URL it cannot parse
becomes a station with one unlabelled feed, which still works.

## Where else it looks

In order, first hit wins:

1. `--station-dir` on the command line
2. `QAMCAST_STATIONS` in the environment
3. `"stations"` in `qamcast.local.json`, beside the project
4. this folder

`qamcast.local.json` is gitignored, so a folder of playlists kept elsewhere on
your machine stays a fact about your machine:

```json
{ "stations": "D:\\Radio Stream" }
```
