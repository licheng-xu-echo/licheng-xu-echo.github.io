# Publication sync

The weekly GitHub Actions workflow discovers publications from Li-Cheng Xu's
public Google Scholar profile. OpenAlex is used to enrich an exact title match
with DOI, full author names, venue, volume, issue, pages, date, and abstract.

The workflow never commits directly to the default branch. It creates or
updates `automation/publication-sync`, then opens a pull request for review.
If no title or journal-version update is found, it does nothing.

## Manual run

Open **Actions → Update publications → Run workflow** in GitHub. To test
locally:

```bash
python -m pip install -r scripts/publication_sync/requirements.txt
python -m unittest discover -s scripts/publication_sync/tests -v
python scripts/publication_sync/sync_publications.py --dry-run
```

Settings live in `.github/publication-sync.json`. Add a normalized or ordinary
paper title to `excluded_titles` if a Scholar-profile item should not be added
to the website.

GitHub repository settings must allow Actions to create pull requests:
**Settings → Actions → General → Workflow permissions → Allow GitHub Actions
to create and approve pull requests**.

## Intentional limits

Google Scholar has no supported public metadata API, so its public profile HTML
can occasionally be rate-limited or changed. The script fails safely in that
case. It does not fall back to an author-name-only search because that can add
another researcher's paper.

Preview images, publisher PDFs, and the `selected` flag require editorial
judgment and are left for the pull-request review. Existing custom BibTeX fields
are preserved when a preprint is upgraded to a journal record.
