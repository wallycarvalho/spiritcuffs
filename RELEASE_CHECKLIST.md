# Release Checklist — spiritcuffs 0.1.0

One-time publishing runbook. Accounts are ready (PyPI + npm); repo URL is
`https://github.com/wallycarvalho/spiritcuffs.git`.

## 0. Pre-flight (before anything public)

- [ ] Full suite green: `uv run --no-project python -m pytest -q` (195+ tests)
- [ ] `uv run --no-project python -m ruff check spc tests`
- [ ] `uv build` produces wheel + sdist
- [ ] Wheel installs in a scratch venv; `spc version`, `spc setup --help`, `spc init` smoke-tested from the artifact
- [ ] README quickstart works from a clean shell (pipx and npx paths)
- [ ] No secrets in the tree: `grep -r "sk-\|ghp_\|github_pat_" . --exclude-dir=.git --exclude-dir=__pycache__` (plus check `.spc/` is gitignored and `~/.spc/config.toml` never referenced in tests)

## 1. GitHub repo

- [ ] Create `wallycarvalho/spiritcuffs` (public)
- [ ] `git remote add origin https://github.com/wallycarvalho/spiritcuffs.git && git push -u origin main`
- [ ] Add repo topics: `code-review`, `llm`, `pre-push`, `developer-tools`
- [ ] Enable Issues; add a CONTRIBUTING.md stub (optional, v1.1)
- [ ] GitHub Actions (optional, v1.1): test + lint workflow

## 2. PyPI

- [ ] Create an API token: pypi.org → Account → API tokens (scope to `spiritcuffs` after first upload)
- [ ] First publish: `uv publish` (or `uv publish --token $PYPI_TOKEN`)
- [ ] Verify on pypi.org/project/spiritcuffs — description renders, license correct, metadata links point at the GitHub repo
- [ ] Smoke: `pipx install spiritcuffs && spc version && spc setup` in a temp dir
- [ ] Smoke: `uvx spiritcuffs@latest version` (the exact path the npm wrapper uses)

## 3. npm

- [ ] `cd npm && npm publish` (npm account: your `wallycarvalho` or org account)
- [ ] Verify the wrapper resolves on a machine with uvx: `npx spiritcuffs version`
- [ ] npm package page shows the repo URL and the single `bin/cli.js` file

## 4. Post-launch verification

- [ ] Full end-to-end on a fresh clone of a real public repo: `uvx spiritcuffs init` → push → review runs → history logs
- [ ] `spc setup` interactive wizard end-to-end with a real key (you, not CI)
- [ ] `spc setup -y` non-interactive against a fake key: fail-soft warning, exit 0
- [ ] Watch for: name collisions / typosquats, PyPI metadata rendering issues
- [ ] Announce only after the end-to-end path is verified

## 5. Immediately after

- [ ] Tag: `git tag v0.1.0 && git push --tags`
- [ ] CHANGELOG.md "0.1.0" section already covers steps 1–3 (verify)
- [ ] Open v1.1 tracking issue: npm-wrapper improvements, keyring storage, `spc history` accept/dismiss marking
