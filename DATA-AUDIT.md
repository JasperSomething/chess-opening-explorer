# Lumbra full-collection audit

Release: 2026-07-08. Full download and extraction completed. Archive: 1,584,100,410 bytes; PGN: 8,625,140,207 bytes.

**Actual header count: 10,355,488 games.** The user selected all games; no Elo cutoff is to be applied.

| Header inventory | Games |
|---|---:|
| White wins | 3,966,202 |
| Draws | 3,217,700 |
| Black wins | 3,171,586 |
| Both ratings present | 8,787,920 |
| One or both ratings missing | 1,567,568 |
| Both players 2200+ (informational only) | 2,823,189 |
| Both players 2400+ (informational only) | 863,790 |
| Event/site contains “corr” | 237,549 |
| Event/site contains “rapid” | 165,401 |
| Event/site contains “blitz” | 126,003 |

The first record has Event “Corr”. The bundle's OTB label does not establish that all games are classical over-the-board games. The substring counts above are warning signals, not definitive classification: labels can be ambiguous and categories may overlap. Preserve the user's all-games selection unless they change it.

This audit examined headers only. It did not independently deduplicate, validate legal moves, or build the position graph. Source names/ratings are supplied by the publisher. The detailed local output is data/lumbra-audit.json; it is excluded from Git because it includes a machine-specific path.

The bulk importer remains unfinished. Reuse all previously cached matching Lichess statistics when implementing it. See HANDOFF.md.
