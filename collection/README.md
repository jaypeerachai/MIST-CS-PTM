# Data collection

This folder contains the scripts and saved data for selecting PTM IDs and finding candidate downstream repositories.

- [ptm_catalogue](ptm_catalogue/README.md): Uses OpenRouter usage and catalogue data to select five PTM authors and 199 IDs for repository discovery.
- [repositories](repositories/README.md): Searches GitHub using those IDs, applies repository and release filters, verifies candidate files, and prepares fixed repository checkouts for MIST.

Both folders support new online collection and reproduction from saved inputs. See their READMEs for commands and reproducibility notes.
