# Captures

Every file here is a real capture of the live deployment
`https://waixxctuxe5ojzlndpci5wvpqi0bixad.lambda-url.us-east-1.on.aws/`
(Lambda Function URL in front of the AgentCore Runtime). Nothing is mocked, generated, or
replayed from cache: the run was a live `POST /api/run/bill_02` with `mock:false`,
`runtime:true` (checked at `/api/health`), the price file was fetched live (badge `LIVE`,
39.1 MB, sha256 `4b79577f0859aadc`), and the letter and the guard refusal came back from
Bedrock during the capture. Bill: `bill_02`, Norman Regional Health System, outpatient
diagnostics. Patient is fictional and the page says so in the footer of every frame.

Capture tool: Playwright 1.x (Python) driving headless Chromium 148.0.7778.96, mobile
context — viewport 390x844 CSS px, `device_scale_factor` 3, `is_mobile` true, `has_touch`
true, iPhone 17.5 Safari user agent. No dev tools, no OS chrome in any pixel.
Capture session: 2026-09-14 08:17 to 08:21 America/Chicago (one live pass, 238 s).

| File | Size | What it shows | Viewport | Captured (America/Chicago) | Tool |
|---|---|---|---|---|---|
| `hero_phone.png` | 238 KB, 1170x2236 px | The Decision Card scene: "What do you want to do?", **$179.60 at stake**, the situation line, the three options with "Send the dispute letter" recommended, and the 2026-09-14 deadline with the link to the hospital's published price file. | 390x844 CSS px @3x | 2026-09-14 08:19 | Playwright screenshot |
| `audit_row_phone.png` | 235 KB, 1170x1993 px | The audit scene: the banner "1 charge on this statement does not match what Norman Regional Health System publishes", the five read lines, and the flagged row 71046 Chest X-ray with Bill says $449.00 / Their file says $269.40, +179.60 over the hospital's own cash price, and the file row citation (row 14756, fetched 2026-09-13). | 390x844 CSS px @3x | 2026-09-14 08:19 | Playwright screenshot |
| `flow.gif` | 557 KB, 390x844 px, 273 frames, 15 fps, 18.2 s | The whole user story on the live link: gallery tap, the statement being read, the live price-file download with the sha256 line and the LIVE badge, the audit banner and the flagged row, the Decision Card, the drafted dispute letter, and the chat where "just pay it" comes back with `pay_bill()` **DENIED**. | 390x844 CSS px (1x frames) | 2026-09-14 08:17-08:21 | Playwright frame sequence + ffmpeg 7.1.1 |

Both PNGs are the untouched capture with one run of blank background rows removed between
the last content block and the sticky footer (hero 296 px of 2532, audit 539 px of 2532, at
3x). Nothing was redrawn, rescaled, or recomposed, and the "Patients fictional" footer is
still on screen in both. The untrimmed originals are
`_runs/2026-09-14_readme_visual/capture/stills/*_untrimmed.png`.

No `hero_phone@2x.png`: `hero_phone.png` is already a 3x capture (1170 px wide) of a 390 px
viewport, so the README can render it at width 390 with no upscaling.

No `guard.gif`: the `pay_bill()` DENIED badge and its reason are held on screen for 2.5 s at
the end of `flow.gif` and are readable at 390 px, so a second GIF would repeat it.

## How flow.gif was built

The live run takes 238 s (Bedrock latency: 85 s to read the statement, 38 s to audit, 62 s
to draft the letter, 29 s for the guard reply). Frames were captured continuously at ~180 ms
during waits and ~70 ms during each beat, then the waiting stretches were subsampled and each
readable beat was held: sha line 2.0 s, audit banner 1.6 s, flagged row 1.9 s, Decision Card
2.2 s, letter 2.0 s, DENIED trace 2.5 s. The selected frames were copied in order into
`_runs/2026-09-14_readme_visual/capture/seq/` and encoded at a fixed 15 fps, so playback
speed is set by which frames were kept, not by a `setpts` filter.

Scripts: `_runs/2026-09-14_readme_visual/capture/capture_live.py` (capture) and
`build_gif.py` (assembly). The two ffmpeg commands it runs:

```
ffmpeg -y -framerate 15 -i seq/%05d.png \
  -vf "fps=15,scale=390:-1:flags=lanczos,palettegen=max_colors=192:stats_mode=diff" palette.png

ffmpeg -y -framerate 15 -i seq/%05d.png -i palette.png \
  -lavfi "fps=15,scale=390:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" \
  -loop 0 flow.gif
```

Verified with `ffprobe`: 390x844, 273 frames, duration 18.200 s, 569,991 bytes (0.57 MB,
well under the 8 MB limit). Five frames extracted at 4.5 s, 8.0 s, 11.5 s, 14.0 s and 17.5 s were opened and checked
for readability.

Raw frames (1112 PNGs, 61 MB), the Playwright webm (4.3 MB), the still candidates, the
milestone log and the verification frames stay in
`_runs/2026-09-14_readme_visual/capture/`.
