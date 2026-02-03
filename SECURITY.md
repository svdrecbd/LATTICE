# Security Notes

LATTICE is designed for consent‑based measurement. Please keep secrets and logs private.

## Secrets

- **Never commit real secrets.**
- Use `config.3endpoints.template.json` as a template and keep local configs untracked.
- Rotate the shared secret if you suspect any exposure.

### Rotate secret

```bash
python scripts/rotate_secret.py
```

Update servers with:
```bash
export LATTICE_SECRET_HEX=<new_secret_hex>
```

## Logs

- JSONL logs can contain sensitive timing patterns. Treat them as private.
- Do not commit `*.jsonl` files.

## Reporting

If you find a security issue or privacy concern, open an issue with details or reach out privately if needed.
