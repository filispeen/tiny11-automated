#!/usr/bin/env python3
"""
Unit tests for scripts/uup_downloader.py
Compatible with both pytest and standard unittest.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from uup_downloader import (
    DirectWindowsUpdateClient,
    UUPDownloader,
    clean_uup_filename,
    normalize_language,
    resolve_edition_info,
)


class TestUUPDownloader(unittest.TestCase):
    def test_normalize_language(self):
        self.assertEqual(normalize_language("English (United States)"), "en-us")
        self.assertEqual(normalize_language("Ukrainian"), "uk-ua")
        self.assertEqual(normalize_language("uk-ua"), "uk-ua")
        self.assertEqual(normalize_language("English International"), "en-gb")
        self.assertEqual(normalize_language("Portuguese (Brazil)"), "pt-br")
        self.assertEqual(normalize_language("Serbian (Latin)"), "sr-latn-rs")
        self.assertEqual(normalize_language(""), "en-us")

        with self.assertRaises(ValueError):
            normalize_language("Klingon")

    def test_resolve_edition_info(self):
        home = resolve_edition_info(1)
        self.assertEqual(home["uup_edition"], "CORE")
        self.assertIsNone(home["virtual_edition"])

        edu = resolve_edition_info(4)
        self.assertEqual(edu["uup_edition"], "PROFESSIONAL")
        self.assertEqual(edu["virtual_edition"], "Education")

        pro = resolve_edition_info(6)
        self.assertEqual(pro["uup_edition"], "PROFESSIONAL")
        self.assertIsNone(pro["virtual_edition"])

        pron = resolve_edition_info(7)
        self.assertEqual(pron["uup_edition"], "PROFESSIONALN")
        self.assertIsNone(pron["virtual_edition"])

        with self.assertRaises(ValueError):
            resolve_edition_info(2)

    def test_direct_iso_url_skips_uup(self):
        downloader = UUPDownloader()
        meta = downloader.resolve_and_prepare(
            version="24H2",
            update_id="https://software.download.prss.microsoft.com/dbazure/Win11_24H2_English_x64.iso",
        )
        self.assertTrue(meta.get("is_direct_iso"))
        self.assertEqual(meta.get("files_count"), 0)

    def test_clean_uup_filename(self):
        self.assertEqual(
            clean_uup_filename("Microsoft-Windows-EditionSpecific-Core-Package~31bf3856ad364e35~amd64~~.esd"),
            "Microsoft-Windows-EditionSpecific-Core-Package-amd64.esd",
        )
        self.assertEqual(
            clean_uup_filename("metadataesd_core_en-us.esd", sha256_capable=False),
            "core_en-us.esd",
        )

    def test_filter_files_for_tiny11_standard_vs_core(self):
        sample_files = {
            "core_en-us.esd": {"size": 318000000, "sha1": "aaa", "url": "http://example.com/core.esd"},
            "Edge.wim": {"size": 180000000, "sha1": "bbb", "url": "http://example.com/edge.wim"},
            "Windows11.0-KB5043080-x64.AggregatedMetadata.cab": {
                "size": 3000000,
                "sha1": "ccc",
                "url": "http://example.com/agg.cab",
            },
            "DesktopDeployment.cab": {
                "size": 7000000,
                "sha1": "ddd",
                "url": "http://example.com/dd.cab",
            },
            "Windows11.0-KB5030509-x64.cab": {
                "size": 58000,
                "sha1": "eee",
                "url": "http://example.com/ekb.cab",
            },
            "Windows11.0-KB5134133-x64.msu": {
                "size": 4879000000,
                "sha1": "fff",
                "url": "http://example.com/lcu.msu",
            },
            "Windows11.0-KB5066128-x64-NDP481.cab": {
                "size": 106000000,
                "sha1": "111",
                "url": "http://example.com/ndp.cab",
            },
        }

        kept_std, skipped_std = UUPDownloader.filter_files_for_tiny11(sample_files, include_lcu=True)
        self.assertIn("core_en-us.esd", kept_std)
        self.assertIn("Windows11.0-KB5030509-x64.cab", kept_std)
        self.assertIn("Windows11.0-KB5134133-x64.msu", kept_std)
        self.assertIn("Windows11.0-KB5066128-x64-NDP481.cab", kept_std)
        self.assertNotIn("Edge.wim", kept_std)
        self.assertNotIn("Windows11.0-KB5043080-x64.AggregatedMetadata.cab", kept_std)
        self.assertNotIn("DesktopDeployment.cab", kept_std)
        self.assertEqual(len(skipped_std), 3)

        kept_core, skipped_core = UUPDownloader.filter_files_for_tiny11(sample_files, include_lcu=False)
        self.assertIn("core_en-us.esd", kept_core)
        self.assertIn("Windows11.0-KB5030509-x64.cab", kept_core)
        self.assertNotIn("Windows11.0-KB5134133-x64.msu", kept_core)
        self.assertNotIn("Windows11.0-KB5066128-x64-NDP481.cab", kept_core)
        self.assertNotIn("Edge.wim", kept_core)
        self.assertEqual(len(skipped_core), 5)

    def test_direct_wu_client_helpers(self):
        token = DirectWindowsUpdateClient.generate_device_token()
        self.assertTrue(isinstance(token, str) and len(token) > 100)
        self.assertEqual(DirectWindowsUpdateClient.branch_from_build("10.0.26300.9725"), "ge_release")
        self.assertEqual(DirectWindowsUpdateClient.branch_from_build("10.0.22631.1"), "ni_release")
        attrs = DirectWindowsUpdateClient.compose_device_attributes(build="26300.1", ring="RETAIL")
        self.assertIn("CurrentBranch=ge_release", attrs)
        self.assertIn("IsRetailOS=1", attrs)

    def test_resolve_and_prepare_with_fallback(self):
        downloader = UUPDownloader()
        mock_files = {
            "core_en-us.esd": {
                "size": 300000000,
                "sha1": "1234567890abcdef1234567890abcdef12345678",
                "url": "https://tlu.dl.delivery.mp.microsoft.com/filestreamingservice/files/core.esd",
            },
            "Edge.wim": {
                "size": 170000000,
                "sha1": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "url": "https://tlu.dl.delivery.mp.microsoft.com/filestreamingservice/files/edge.wim",
            },
            "Windows11.0-KB5134133-x64.msu": {
                "size": 4800000000,
                "sha1": "cafebabecafebabecafebabecafebabecafebabe",
                "url": "https://tlu.dl.delivery.mp.microsoft.com/filestreamingservice/files/lcu.msu",
            },
        }

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_path = Path(tmp_str)
            with patch.object(
                downloader,
                "resolve_files_direct_wu",
                side_effect=RuntimeError("Simulated WU timeout"),
            ), patch.object(
                downloader,
                "resolve_files_uupdump_api",
                return_value={
                    "updateName": "Windows 11 Insider Preview (26300.9725)",
                    "arch": "amd64",
                    "build": "26300.9725",
                    "files": mock_files,
                },
            ):
                aria2_file = tmp_path / "uup_aria2.txt"
                meta_file = tmp_path / "uup_metadata.json"
                out_dir = tmp_path / "UUPs"

                meta = downloader.resolve_and_prepare(
                    version="26H2",
                    update_id="a9e9673e-1444-4bec-9445-1d294d11bb4e",
                    language="Ukrainian",
                    image_index=4,
                    include_lcu=False,
                    output_dir=out_dir,
                    aria2_out=aria2_file,
                    metadata_out=meta_file,
                )

                self.assertEqual(meta["resolution_source"], "uupdump_api_fallback")
                self.assertEqual(meta["language_code"], "uk-ua")
                self.assertEqual(meta["uup_edition"], "PROFESSIONAL")
                self.assertEqual(meta["virtual_edition"], "Education")
                self.assertEqual(meta["files_count"], 1)
                self.assertEqual(meta["skipped_count"], 2)
                self.assertTrue(aria2_file.exists())
                self.assertIn("out=core_en-us.esd", aria2_file.read_text(encoding="utf-8"))
                self.assertEqual(json.loads(meta_file.read_text(encoding="utf-8"))["windows_build"], "26300.9725")


if __name__ == "__main__":
    unittest.main()
