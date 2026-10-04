# DVC + private object storage

Persist `data/` across machines and GitHub Actions **without** committing raw OMI dumps to git (RNF-03).  
DVC versions the files; the remote (Cloudflare R2 preferred, or AWS S3) stores the bytes.  
Keep the existing train fingerprint in `models/baseline_latest/dataset.json` (RF-12) — DVC does not replace it.

## What you get

- Local / CI: `dvc pull` → history under `data/`
- After pipeline: `dvc push` → remote keeps snapshots
- Repo stays clean: only small `.dvc` pointer files in git

Track `data/processed/features_latest.jsonl`, `data/processed/omi/boundaries/H501.geojson`, **and** `data/raw/` (OMI CSVs under `data/raw/omi/`).  
You download CSVs manually from Fisconline; then `dvc add` / `dvc push` so CI can pull them.  
`.dvcignore` may still exclude HTML leftovers; OMI path does not scrape HTML.

---



## 1. Create a private bucket (R2 recommended)

**Cloudflare R2** (S3-compatible, free tier, no egress fees):

1. Cloudflare dashboard → **R2** → **Create bucket** (e.g. `rent-tracker-data`).
2. **Manage R2 API Tokens** → create a token with Object Read & Write on that bucket.
3. Note: Account ID, Access Key ID, Secret Access Key, bucket name.
4. Endpoint form: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

**AWS S3 alternative:** create a private bucket in a cheap region; stay inside free tier / set billing alerts.

Never make the bucket public. Never commit credentials.

---



## 2. Install DVC locally

From the repo root (venv active):

```bash
pip install 'dvc[s3]'
```

(`dvc[s3]` works for both AWS S3 and R2.)

---



## 3. Initialize DVC and track data

Only after you already have processed features locally (e.g. `run_pipeline.py` on real CSVs):

```bash
dvc init
dvc add data/processed/features_latest.jsonl
dvc add data/processed/omi/boundaries/H501.geojson   # PIP zones for /meta/zona-from-point
git add data/processed/features_latest.jsonl.dvc \
  data/processed/omi/boundaries/H501.geojson.dvc \
  data/processed/omi/boundaries/.gitignore \
  .dvc .dvcignore .gitignore
git commit -m "Add DVC tracking for features_latest and OMI boundaries"
```

`dvc add` creates a `.dvc` pointer. The real JSONL stays out of git via the root `.gitignore` (`data/processed/*` + exceptions for `*.dvc`). DVC may also create `data/processed/.gitignore`; if that file is missing, omit it from `git add` — that is normal when the root ignore already covers the data.

---



## 4. Configure the remote

**Do not** put Access Key / Secret into `.dvc/config` (that file is committed).  
Only the bucket URL + endpoint go in git; credentials come from env vars at `push`/`pull` time.

### 4a. Non-secret remote (commit this)

```bash
dvc remote add -d r2 s3://rent-tracker-data/dvc
dvc remote modify r2 endpointurl https://<ACCOUNT_ID>.r2.cloudflarestorage.com
git add .dvc/config
git commit -m "Configure DVC remote (R2)"
```

No `export` needed for these two commands.  
For plain AWS S3: skip `endpointurl`; use a normal `s3://bucket/prefix`.

### 4b. Credentials (env vars — before every push/pull)

```bash
export AWS_ACCESS_KEY_ID='<ACCESS_KEY_ID>'
export AWS_SECRET_ACCESS_KEY='<SECRET_ACCESS_KEY>'
# Optional: usually redundant if endpointurl is already in .dvc/config
# export AWS_ENDPOINT_URL='https://<ACCOUNT_ID>.r2.cloudflarestorage.com'
```

Store these outside the repo (password manager / shell profile / GitHub Actions secrets).  
Never commit them; never keep an `API_token` file in the project.

### Optional: keys only on this machine (`--local`)

If you prefer not to `export` every session:

```bash
dvc remote modify --local r2 access_key_id <ACCESS_KEY_ID>
dvc remote modify --local r2 secret_access_key <SECRET_ACCESS_KEY>
```

Writes to `.dvc/config.local` (gitignored). Still never put secrets in the committed `.dvc/config`.

---



## 5. First push

With credentials exported (or `--local` keys set):

```bash
dvc push
```

Check in the R2/S3 console that objects appeared under the `dvc/` prefix.

---



## 6. GitHub Actions secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → add:


| Secret                  | Value                                                                   |
| ----------------------- | ----------------------------------------------------------------------- |
| `AWS_ACCESS_KEY_ID`     | R2 or S3 access key                                                     |
| `AWS_SECRET_ACCESS_KEY` | R2 or S3 secret                                                         |
| `AWS_ENDPOINT_URL`      | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` (R2 only; omit for AWS) |


---



## 7. Wire `daily_monitoring.yml` (minimal)

In `[.github/workflows/daily_monitoring.yml](../.github/workflows/daily_monitoring.yml)`, after install and **before** the pipeline:

```yaml
      - name: Install DVC
        run: pip install 'dvc[s3]'

      - name: Pull data history
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          AWS_ENDPOINT_URL: ${{ secrets.AWS_ENDPOINT_URL }}
        run: dvc pull --force
        continue-on-error: true   # first run: remote may be empty
```

**After** OMI load / features (and before or after artifact upload):

```yaml
      - name: Push data history
        if: success()
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          AWS_ENDPOINT_URL: ${{ secrets.AWS_ENDPOINT_URL }}
        run: |
          dvc add data/raw
          dvc add data/processed/features_latest.jsonl
          dvc push data/raw.dvc data/processed/features_latest.jsonl.dvc
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/raw.dvc data/processed/features_latest.jsonl.dvc
          if [ -f data/raw/.gitignore ]; then git add data/raw/.gitignore; fi
          git diff --staged --quiet || git commit -m "dvc: update OMI data [skip ci]"
          git push
```

Notes for the push step:

- Needs `permissions: contents: write` on the job (or a PAT) so the bot can commit the updated `.dvc` pointers.
- Pull restores `data/raw/omi/` CSVs + `features_latest` → reload → features → push.
- `[skip ci]` avoids a commit loop on `ci.yml`.
- If you prefer **no** git commits from Actions: push only with `dvc push` and update the `.dvc` files locally — simpler, slightly less automated.

Minimal alternative (no git write from CI): only `dvc pull` / `dvc add` + `dvc push`, and commit `.dvc` changes yourself after a local run.

---



## 8. Verify

**Local (fresh clone / empty** `data/`**):**

```bash
export AWS_ACCESS_KEY_ID='...'
export AWS_SECRET_ACCESS_KEY='...'
dvc pull
ls -la data/raw/omi/ data/processed/features_latest.jsonl
.venv/bin/python run_pipeline.py --skip-train -v
```

**CI:** run *omi-monitoring* after pushing new OMI CSVs to DVC.

**Interview check:** show `.dvc` pointer in git + private bucket + `dataset.json` SHA-256 next to the model.

---



## Day-to-day commands

```bash
export AWS_ACCESS_KEY_ID='...'
export AWS_SECRET_ACCESS_KEY='...'
dvc pull
# After manual OMI CSV download into data/raw/omi/ (*VALORI*.csv; provincia RM OK):
.venv/bin/python run_pipeline.py -v
dvc add data/raw
dvc add data/processed/features_latest.jsonl
dvc push
git add data/raw.dvc data/processed/features_latest.jsonl.dvc
git add data/raw/.gitignore 2>/dev/null || true
git commit -m "dvc: refresh OMI data"
```

Then push **code + `models/baseline_latest/`** and redeploy Render (see [`render.md`](render.md)).

---



## Notes

- Object storage ≠ SQL database. Stay on JSONL + DVC until you need queryable history.
- Do not publish raw OMI dumps; bucket must stay private.
- Download from Fisconline stays manual; DVC only persists what you already placed under `data/raw/omi/`.
- Cost target (RNF-01): R2 free tier is enough for this portfolio volume.
- Done when: two CI (or machine) runs share the same processed history via `dvc pull`/`dvc push`.

