# Local verification report

POC verified at 2026-09-12T23:21:08.956687+00:00.

Both current Explorer endpoints returned HTTP 401 without authentication. Authenticated requests succeeded using the supplied local token file. The token was never stored in the repository.

A live HTTP 429 was observed during the first shallow crawl. The client waited 60 seconds and resumed. Default spacing is now three seconds, with further adaptive slowdown.

Live POC covered the initial position, all qualifying first moves, and selected lines through four plies. 256- and 512-move requests returned the same continuation sets at the initial position. Two transposing move orders and direct canonical FEN queries returned matching continuation sets in both databases.

| Initial position | Games | Continuations |
|---|---:|---:|
| lichess | 7,826,583,724 | 20 |
| masters | 2,879,587 | 20 |

12 offline tests pass. SQLite integrity and foreign-key checks pass. Browser checks verified piece selection and moving, continuation buttons, line display, back/forward, real counts, major highlights, and square board layout.

The complete graph is **not yet downloaded**. The unrestricted crawler was launched, with progress in `crawl.log`; inspect its timestamp to establish whether it is still running. Use `python explorer.py status` for current coverage; a growing frontier means the eventual graph size is still unknown. The UI remains usable with partial data.

The private GitHub repository is `JasperSomething/chess-opening-explorer`. Git identity is configured locally using the authenticated account and its GitHub no-reply address. No global Git configuration was changed.
