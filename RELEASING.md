# Releasing

A `vX.Y.Z` tag runs `.github/workflows/release.yml`, which publishes three artifacts from the same commit:

| Artifact | Where | Auth |
|---|---|---|
| Python package `codex-images-mcp` | PyPI | Trusted Publishing (OIDC, no stored token) |
| Docker image `codex-images-mcp` (amd64, arm64) | `ghcr.io/jlmoreno00/codex-images-mcp` | `GITHUB_TOKEN` |
| Pi extension `pi-codex-images` | npm, with provenance | `NPM_TOKEN` secret |

Each job fails if the tag does not match the version it publishes.

## One-time setup

1. **PyPI**: on https://pypi.org/manage/account/publishing/ add a *pending publisher* for project `codex-images-mcp`, owner `JlMoreno00`, repository `codex-images-mcp`, workflow `release.yml`, environment `pypi`. Then create the `pypi` environment under **Settings → Environments** (optionally with required reviewers).
2. **npm**: create a granular access token that can publish `pi-codex-images` and add it as the repository secret `NPM_TOKEN`.
3. **GHCR**: nothing to configure. After the first push, open the package settings and set its visibility to public.

## Each release

1. Bump the version in all three places:
   - `pyproject.toml` (`version`)
   - `src/codex_images_mcp/__init__.py` (`__version__`)
   - `packages/pi-codex-images/package.json` (`version`, then `npm install` to refresh the lockfile)
2. Add the release notes to `CHANGELOG.md`.
3. Commit, push to `main`, wait for CI to pass.
4. Tag and push the tag:

   ```bash
   git tag v0.1.1
   git push origin v0.1.1
   ```

5. Watch https://github.com/JlMoreno00/codex-images-mcp/actions, then check a clean install:

   ```bash
   uvx --from codex-images-mcp codex-images-mcp --version
   docker run --rm ghcr.io/jlmoreno00/codex-images-mcp:latest codex-images-mcp --version
   npm view pi-codex-images version
   ```
