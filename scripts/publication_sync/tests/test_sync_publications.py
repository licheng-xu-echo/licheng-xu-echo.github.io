import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "sync_publications.py"
SPEC = importlib.util.spec_from_file_location("sync_publications", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


class PublicationSyncTests(unittest.TestCase):
    def test_normalized_title_ignores_bibtex_braces_and_punctuation(self):
        left = "Towards {Data}-{Driven} Design of C–H Functionalization"
        right = "Towards data-driven design of C-H functionalization"
        self.assertEqual(sync.normalized_title(left), sync.normalized_title(right))

    def test_parse_nested_bibtex(self):
        content = "---\n---\n@article{x,\n title = {A {Nested} Title},\n abstract = {A {test}.},\n}\n"
        entries = sync.parse_bib_entries(content)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].key, "x")
        self.assertEqual(entries[0].fields["title"], "A {Nested} Title")

    def test_new_entries_are_inserted_after_front_matter(self):
        original = "---\n---\n@article{old,\n title = {Old},\n}\n"
        updated = sync.insert_new_entries(original, ["@article{new,\n  title = {New},\n}"])
        self.assertTrue(updated.startswith("---\n---\n@article{new,"))
        self.assertIn("@article{old,", updated)

    def test_upgrade_preserves_custom_fields(self):
        raw = (
            "@misc{x,\n"
            " title = {Paper},\n journal = {ChemRxiv},\n"
            " doi = {10.26434/chemrxiv.1/v1},\n preview = {paper.gif},\n}"
        )
        entry = sync.BibEntry(0, len(raw), "misc", "x", raw, sync.parse_bib_fields(raw))
        updated = sync.upgrade_entry(
            entry,
            {
                "journal": "Nature Chemistry",
                "doi": "10.1000/example",
                "url": "https://doi.org/10.1000/example",
                "year": "2027",
            },
        )
        self.assertIn("preview = {paper.gif}", updated)
        self.assertIn("journal = {Nature Chemistry}", updated)
        self.assertIn("doi = {10.1000/example}", updated)

    def test_preprint_detection(self):
        self.assertTrue(sync.is_preprint_doi("https://doi.org/10.48550/arXiv.2601.03689"))
        self.assertFalse(sync.is_preprint_doi("10.1038/s41467-026-77230-8"))

    def test_openalex_metadata_renders_a_complete_article(self):
        work = sync.ScholarWork(
            title="A New Paper",
            authors_summary="LC Xu, A Researcher",
            venue_summary="Example Journal, 2027",
            year="2027",
            detail_url="https://scholar.google.com/example",
            scholar_id="abc123",
        )
        openalex = {
            "type": "article",
            "publication_date": "2027-04-02",
            "doi": "https://doi.org/10.1000/example",
            "authorships": [
                {"author": {"display_name": "Li-Cheng Xu"}},
                {"author": {"display_name": "A. Researcher"}},
            ],
            "primary_location": {"source": {"display_name": "Example Journal"}},
            "biblio": {"volume": "3", "issue": "2", "first_page": "10", "last_page": "18"},
            "abstract_inverted_index": {"A": [0], "test": [1], "abstract": [2]},
        }
        metadata = sync.metadata_from_sources(work, {}, openalex)
        rendered = sync.render_bib_entry(metadata, "xu_new_2027")
        self.assertIn("author = {Li-Cheng Xu and A. Researcher}", rendered)
        self.assertIn("journal = {Example Journal}", rendered)
        self.assertIn("pages = {10--18}", rendered)
        self.assertIn("doi = {10.1000/example}", rendered)
        self.assertIn("abstract = {A test abstract}", rendered)


if __name__ == "__main__":
    unittest.main()
