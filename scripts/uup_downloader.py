#!/usr/bin/env python3
"""
UUP Direct Resolver & Downloader for Tiny11 Automated
Author: kelexine (https://github.com/kelexine)

Resolves Windows 11 UUP file sets (ESDs, FOD CABs, and Cumulative Updates)
directly from Microsoft Windows Update ClientWebService (fe3cr.delivery.mp.microsoft.com)
with automatic fallback to the public UUP Dump API (api.uupdump.net).
Zero PHP dependencies. Generates aria2c input files for high-speed parallel downloading.
"""

import argparse
import base64
import datetime
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# Language mapping: supports Microsoft dropdown labels, plain names, and UUP lang codes
LANGUAGE_MAP: Dict[str, str] = {
    "arabic": "ar-sa",
    "arabic (saudi arabia)": "ar-sa",
    "brazilian portuguese": "pt-br",
    "portuguese (brazil)": "pt-br",
    "bulgarian": "bg-bg",
    "chinese simplified": "zh-cn",
    "chinese (simplified)": "zh-cn",
    "chinese traditional": "zh-tw",
    "chinese (traditional)": "zh-tw",
    "croatian": "hr-hr",
    "czech": "cs-cz",
    "danish": "da-dk",
    "dutch": "nl-nl",
    "english": "en-us",
    "english (united states)": "en-us",
    "englishunitedstates": "en-us",
    "english international": "en-gb",
    "english (united kingdom)": "en-gb",
    "englishinternational": "en-gb",
    "estonian": "et-ee",
    "finnish": "fi-fi",
    "french": "fr-fr",
    "french (france)": "fr-fr",
    "french canadian": "fr-ca",
    "french (canada)": "fr-ca",
    "frenchcanadian": "fr-ca",
    "german": "de-de",
    "greek": "el-gr",
    "hebrew": "he-il",
    "hungarian": "hu-hu",
    "italian": "it-it",
    "japanese": "ja-jp",
    "korean": "ko-kr",
    "latvian": "lv-lv",
    "lithuanian": "lt-lt",
    "norwegian": "nb-no",
    "norwegian (bokmal)": "nb-no",
    "polish": "pl-pl",
    "portuguese": "pt-pt",
    "portuguese (portugal)": "pt-pt",
    "romanian": "ro-ro",
    "russian": "ru-ru",
    "serbian latin": "sr-latn-rs",
    "serbian (latin)": "sr-latn-rs",
    "serbianlatin": "sr-latn-rs",
    "slovak": "sk-sk",
    "slovenian": "sl-si",
    "spanish": "es-es",
    "spanish (spain)": "es-es",
    "spanish (mexico)": "es-mx",
    "spanishmexico": "es-mx",
    "swedish": "sv-se",
    "thai": "th-th",
    "turkish": "tr-tr",
    "ukrainian": "uk-ua",
}

# Maps Tiny11 image_index (1=Home, 4=Education, 6=Pro, 7=Pro N) to UUP base edition and virtual edition
EDITION_INDEX_MAP: Dict[int, Dict[str, Optional[str]]] = {
    1: {
        "uup_edition": "CORE",
        "virtual_edition": None,
        "edition_name": "Home",
        "image_name": "Windows 11 Home",
        "image_flag": "Core",
    },
    4: {
        "uup_edition": "PROFESSIONAL",
        "virtual_edition": "Education",
        "edition_name": "Education",
        "image_name": "Windows 11 Education",
        "image_flag": "Education",
    },
    6: {
        "uup_edition": "PROFESSIONAL",
        "virtual_edition": None,
        "edition_name": "Pro",
        "image_name": "Windows 11 Pro",
        "image_flag": "Professional",
    },
    7: {
        "uup_edition": "PROFESSIONALN",
        "virtual_edition": None,
        "edition_name": "ProN",
        "image_name": "Windows 11 Pro N",
        "image_flag": "ProfessionalN",
    },
}

# Version -> default build prefix and branch for Windows Update queries
VERSION_BUILD_MAP: Dict[str, Tuple[str, str]] = {
    "26H2": ("26300", "ge_release"),
    "25H2": ("26200", "ge_release"),
    "24H2": ("26100", "ge_release"),
    "23H2": ("22631", "ni_release"),
    "22H2": ("22621", "ni_release"),
}

NON_LEAF_UPDATE_IDS = [
    1, 2, 3, 10, 11, 17, 19, 2359974, 2359977, 5143990, 5169043, 5169044, 5169047,
    8788830, 8806526, 9125350, 9154769, 10809856, 23110993, 23110994, 23110995,
    23110996, 23110999, 23111000, 23111001, 23111002, 23111003, 23111004, 24513870,
    28880263, 30077688, 30486944, 59830006, 59830007, 59830008, 60484010, 62450018,
    62450019, 62450020, 69801474, 98959022, 98959023, 98959024, 98959025, 98959026,
    105939029, 105995585, 106017178, 107825194, 117765322, 129905029, 130040030,
    130040031, 130040032, 130040033, 133399034, 138372035, 138372036, 139536037,
    139536038, 139536039, 139536040, 142045136, 158941041, 158941042, 158941043,
    158941044, 159776047, 160733048, 160733049, 160733050, 160733051, 160733055,
    160733056, 161870057, 161870058, 161870059, 296374060, 316003061, 326686062,
    326686063, 327065581, 327072300, 327072305, 333703234,
]


def normalize_language(lang_input: str) -> str:
    """Resolve a human-readable language name or code to a UUP language code (e.g. 'en-us')."""
    if not lang_input:
        return "en-us"
    cleaned = lang_input.strip().lower()
    if cleaned in LANGUAGE_MAP:
        return LANGUAGE_MAP[cleaned]
    if re.match(r"^[a-z]{2,3}-[a-z]{2,4}(-[a-z]{2,4})?$", cleaned):
        return cleaned
    alpha_only = re.sub(r"[^a-z]", "", cleaned)
    if alpha_only in LANGUAGE_MAP:
        return LANGUAGE_MAP[alpha_only]
    raise ValueError(
        f"Unsupported language '{lang_input}'. Pass a valid language name (e.g. 'English (United States)', 'Ukrainian') or code (e.g. 'en-us', 'uk-ua')."
    )


def resolve_edition_info(index: int) -> Dict[str, Optional[str]]:
    """Map Tiny11 image index (1, 4, 6, 7) to UUP edition metadata."""
    if index not in EDITION_INDEX_MAP:
        raise ValueError(
            f"Unsupported image index {index}. Valid indices: {sorted(EDITION_INDEX_MAP.keys())}"
        )
    return EDITION_INDEX_MAP[index]


def clean_uup_filename(name: str, sha256_capable: bool = True) -> str:
    """Port of uupCleanSha256 / uupCleanName from api/get.php."""
    replacements = [
        ("prss_signed_appx_", ""),
        ("~31bf3856ad364e35", ""),
        ("~~.", "."),
        ("~.", "."),
        ("~", "-"),
    ]
    if not sha256_capable:
        replacements = [("cabs_", ""), ("metadataesd_", "")] + replacements
        name = name.lower()
    for old, new in replacements:
        name = name.replace(old, new)
    return name


class DirectWindowsUpdateClient:
    """
    Pure-Python client for Microsoft's ClientWebService SOAP API
    (ported from api/shared/requests.php, api/shared/auths.php, api/fetchupd.php, api/get.php).
    """

    CLIENT_URL = "https://fe3cr.delivery.mp.microsoft.com/ClientWebService/client.asmx"
    SECURED_URL = "https://fe3cr.delivery.mp.microsoft.com/ClientWebService/client.asmx/secured"
    HEADERS = {
        "User-Agent": "Windows-Update-Agent/10.0.10011.16384 Client-Protocol/2.50",
        "Content-Type": "application/soap+xml; charset=utf-8",
    }

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._encrypted_cookie: Optional[str] = None

    @staticmethod
    def generate_device_token() -> str:
        """Port of uupDevice() from api/shared/auths.php."""
        t_header = "13003002c377040014d5bcac7a66de0d50beddf9bba16c87edb9e019898000"
        t_rand = os.urandom(527).hex()
        t_end = "b401"
        t_val = base64.b64encode(bytes.fromhex(t_header + t_rand + t_end)).decode("ascii")
        data_str = f"t={t_val}&p="
        utf16_bytes = "".join(c + "\x00" for c in data_str).encode("ascii")
        return base64.b64encode(utf16_bytes).decode("ascii")

    @staticmethod
    def branch_from_build(build_str: str) -> str:
        """Port of branchFromBuild() from api/shared/requests.php."""
        parts = build_str.split(".")
        major = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else int(parts[0])
        if major in (22621, 22631):
            return "ni_release"
        if major in (26100, 26200, 26300):
            return "ge_release"
        if major in (28000, 28100):
            return "br_release"
        if major == 22000:
            return "co_release"
        return "ge_release"

    @classmethod
    def compose_device_attributes(
        cls,
        build: str = "10.0.26300.1",
        arch: str = "amd64",
        sku: int = 48,
        ring: str = "RETAIL",
        branch: str = "auto",
        flags: Tuple[str, ...] = ("thisonly",),
        target_release: int = -1,
    ) -> str:
        """Port of composeDeviceAttributes() from api/shared/requests.php."""
        if not build.startswith("10.0."):
            build = f"10.0.{build}" if "." in build else f"10.0.{build}.1"
        if branch == "auto":
            branch = cls.branch_from_build(build)

        now_ts = int(time.time())
        ring_up = ring.upper()
        is_retail = 1 if ring_up == "RETAIL" else 0
        flight_enabled = 0 if ring_up == "RETAIL" else 1
        flt_ring = "Retail" if ring_up == "RETAIL" else "External"
        flt_branch = {
            "WIF": "Dev",
            "DEV": "Dev",
            "WIS": "Beta",
            "BETA": "Beta",
            "RP": "ReleasePreview",
            "RELEASEPREVIEW": "ReleasePreview",
            "CANARY": "CanaryChannel",
        }.get(ring_up, "")

        attrib = [
            "App=WU_OS",
            f"AppVer={build}",
            "AttrDataVer=370",
            "AllowInPlaceUpgrade=1",
            "AllowOptionalContent=1",
            "AllowUpgradesWithUnsupportedTPMOrCPU=1",
            "AllowWindowsServerFeatureUpdate=1",
            "BlockFeatureUpdates=0",
            "BranchReadinessLevel=CB",
            "CIOptin=1",
            f"CurrentBranch={branch}",
            f"DataExpDateEpoch_GE26H2={now_ts + 82800}",
            f"DataExpDateEpoch_GE25H2={now_ts + 82800}",
            f"DataExpDateEpoch_GE24H2={now_ts + 82800}",
            f"DataExpDateEpoch_GE24H2Setup={now_ts + 82800}",
            f"DataExpDateEpoch_NI23H2={now_ts + 82800}",
            f"DataExpDateEpoch_NI22H2={now_ts + 82800}",
            f"DataExpDateEpoch_NI22H2Setup={now_ts + 82800}",
            "DataVer_RS5=2000000000",
            "DefaultUserRegion=191",
            "DeviceFamily=Windows.Desktop",
            "DeviceInfoGatherSuccessful=1",
            "EKB19H2InstallCount=1",
            "EKB19H2InstallTimeEpoch=1255000000",
            f"FlightingBranchName={flt_branch}",
            f"FlightRing={flt_ring}",
            "Free=gt64",
            "GStatus_GE26H2=2",
            "GStatus_GE25H2=2",
            "GStatus_GE24H2=2",
            "GStatus_GE24H2Setup=2",
            "GStatus_NI23H2=2",
            "GStatus_NI22H2=2",
            "GStatus_NI22H2Setup=2",
            "GStatus_RS5=2",
            f"GenTelRunTimestamp_19H1={now_ts - 3600}",
            "InstallDate=1438196400",
            "InstallLanguage=en-US",
            "InstallationType=Client",
            "IsDeviceRetailDemo=0",
            f"IsFlightingEnabled={flight_enabled}",
            f"IsRetailOS={is_retail}",
            "LaunchUserOOBE=1",
            "LCUVer=0.0.0.0",
            f"MediaBranch={branch if 'thisonly' in flags else ''}",
            f"MediaVersion={build}",
            "CloudPBR=1",
            "DUScan=1",
            "OEMModel=21F6CTO1WW",
            "OEMModelBaseBoard=21F6CTO1WW",
            "OEMName_Uncleaned=LENOVO",
            "OemPartnerRing=UPSFlighting",
            f"OSArchitecture={arch}",
            f"OSSkuId={sku}",
            "OSUILocale=en-US",
            f"OSVersion={build}",
            "ProcessorIdentifier=Intel64 Family 6 Model 186 Stepping 3",
            "ProcessorManufacturer=GenuineIntel",
            "ProcessorModel=13th Gen Intel(R) Core(TM) i7-1355U",
            "ProductType=WinNT",
            "ReleaseType=Production",
            "SdbVer_GE24H2=2000000000",
            "SdbVer_NI22H2=2000000000",
            "SecureBootCapable=1",
            "TelemetryLevel=3",
            f"TimestampEpochString_GE26H2={now_ts - 3600}",
            f"TimestampEpochString_GE25H2={now_ts - 3600}",
            f"TimestampEpochString_GE24H2={now_ts - 3600}",
            f"TimestampEpochString_GE24H2Setup={now_ts - 3600}",
            "TPMVersion=2",
            "UpdateManagementGroup=2",
            "UpdateOfferedDays=0",
            "UpgEx_GE26H2=Green",
            "UpgEx_GE25H2=Green",
            "UpgEx_GE24H2Setup=Green",
            "UpgEx_GE24H2=Green",
            "UpgEx_NI23H2=Green",
            "UpgEx_NI22H2=Green",
            "UpgradeAccepted=1",
            "UpgradeEligible=1",
            "UserInPlaceUpgrade=1",
            "VBSState=2",
            "Version_RS5=2000000000",
            f"WuClientVer={build}",
            f"FlightUpgradeTarget={target_release}",
            "IsFutureKeyEnabled=1",
        ]
        return html.escape("E:" + "&".join(attrib))

    def get_cookie(self) -> str:
        """Obtain a Windows Update session EncryptedData cookie via SOAP GetCookie."""
        if self._encrypted_cookie:
            return self._encrypted_cookie

        now = datetime.datetime.now(datetime.timezone.utc)
        created = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        expires = (now + datetime.timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        device = self.generate_device_token()

        envelope = f"""<s:Envelope xmlns:a="http://www.w3.org/2005/08/addressing" xmlns:s="http://www.w3.org/2003/05/soap-envelope">
    <s:Header>
        <a:Action s:mustUnderstand="1">http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService/GetCookie</a:Action>
        <a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>
        <a:To s:mustUnderstand="1">https://fe3.delivery.mp.microsoft.com/ClientWebService/client.asmx</a:To>
        <o:Security s:mustUnderstand="1" xmlns:o="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <Timestamp xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                <Created>{created}</Created>
                <Expires>{expires}</Expires>
            </Timestamp>
            <wuws:WindowsUpdateTicketsToken wsu:id="ClientMSA" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd" xmlns:wuws="http://schemas.microsoft.com/msus/2014/10/WindowsUpdateAuthorization">
                <TicketType Name="MSA" Version="1.0" Policy="MBI_SSL">
                    <Device>{device}</Device>
                </TicketType>
            </wuws:WindowsUpdateTicketsToken>
        </o:Security>
    </s:Header>
    <s:Body>
        <GetCookie xmlns="http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService">
            <oldCookie><Expiration>{created}</Expiration></oldCookie>
            <lastChange>{created}</lastChange>
            <currentTime>{created}</currentTime>
            <protocolVersion>2.0</protocolVersion>
        </GetCookie>
    </s:Body>
</s:Envelope>"""

        resp = requests.post(
            self.CLIENT_URL,
            data=envelope.encode("utf-8"),
            headers=self.HEADERS,
            verify=False,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        text = html.unescape(resp.text)
        match = re.search(r"<EncryptedData>(.*?)</EncryptedData>", text)
        if not match:
            raise RuntimeError("Windows Update GetCookie response did not contain EncryptedData")
        self._encrypted_cookie = match.group(1)
        return self._encrypted_cookie

    def sync_updates(
        self,
        check_build: str,
        arch: str = "amd64",
        ring: str = "RETAIL",
        sku: int = 48,
        branch: str = "auto",
        flags: Tuple[str, ...] = ("thisonly",),
    ) -> List[Dict]:
        """
        Call SOAP SyncUpdates on fe3cr.delivery.mp.microsoft.com to retrieve update metadata
        and SHA1/SHA256 file maps directly from Microsoft.
        """
        if not check_build.startswith("10.0."):
            check_build = f"10.0.{check_build}" if "." in check_build else f"10.0.{check_build}.1"
        if branch == "auto":
            branch = self.branch_from_build(check_build)

        enc_data = self.get_cookie()
        now = datetime.datetime.now(datetime.timezone.utc)
        created = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        expires = (now + datetime.timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        cookie_expires = (now + datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        device = self.generate_device_token()

        product_items = [
            f"PN=Client.OS.rs2.{arch}&Branch={branch}&PrimaryOSProduct=1&Repairable=1&V={check_build}&ReofferUpdate=1",
            f"PN=Windows.UpdateStackPackage.{arch}&Name=Update Stack Package&Repairable=1&V={check_build}",
        ]
        products_str = html.escape(";".join(product_items))
        caller_attrib = html.escape(
            "E:Profile=AUv2&Acquisition=1&Interactive=1&IsSeeker=1&SheddingAware=1&Id=MoUpdateOrchestrator"
        )
        dev_attrib = self.compose_device_attributes(
            build=check_build, arch=arch, sku=sku, ring=ring, branch=branch, flags=flags
        )
        non_leaf_xml = "".join(f"<int>{i}</int>" for i in NON_LEAF_UPDATE_IDS)
        sync_current = "true" if "thisonly" in flags else "false"

        envelope = f"""<s:Envelope xmlns:a="http://www.w3.org/2005/08/addressing" xmlns:s="http://www.w3.org/2003/05/soap-envelope">
    <s:Header>
        <a:Action s:mustUnderstand="1">http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService/SyncUpdates</a:Action>
        <a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>
        <a:To s:mustUnderstand="1">https://fe3.delivery.mp.microsoft.com/ClientWebService/client.asmx</a:To>
        <o:Security s:mustUnderstand="1" xmlns:o="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <Timestamp xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                <Created>{created}</Created>
                <Expires>{expires}</Expires>
            </Timestamp>
            <wuws:WindowsUpdateTicketsToken wsu:id="ClientMSA" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd" xmlns:wuws="http://schemas.microsoft.com/msus/2014/10/WindowsUpdateAuthorization">
                <TicketType Name="MSA" Version="1.0" Policy="MBI_SSL">
                    <Device>{device}</Device>
                </TicketType>
            </wuws:WindowsUpdateTicketsToken>
        </o:Security>
    </s:Header>
    <s:Body>
        <SyncUpdates xmlns="http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService">
            <cookie>
                <Expiration>{cookie_expires}</Expiration>
                <EncryptedData>{enc_data}</EncryptedData>
            </cookie>
            <parameters>
                <ExpressQuery>false</ExpressQuery>
                <InstalledNonLeafUpdateIDs>{non_leaf_xml}</InstalledNonLeafUpdateIDs>
                <OtherCachedUpdateIDs/>
                <SkipSoftwareSync>false</SkipSoftwareSync>
                <NeedTwoGroupOutOfScopeUpdates>true</NeedTwoGroupOutOfScopeUpdates>
                <AlsoPerformRegularSync>true</AlsoPerformRegularSync>
                <ComputerSpec/>
                <ExtendedUpdateInfoParameters>
                    <XmlUpdateFragmentTypes>
                        <XmlUpdateFragmentType>Extended</XmlUpdateFragmentType>
                        <XmlUpdateFragmentType>LocalizedProperties</XmlUpdateFragmentType>
                    </XmlUpdateFragmentTypes>
                    <Locales><string>en-US</string></Locales>
                </ExtendedUpdateInfoParameters>
                <ClientPreferredLanguages/>
                <ProductsParameters>
                    <SyncCurrentVersionOnly>{sync_current}</SyncCurrentVersionOnly>
                    <DeviceAttributes>{dev_attrib}</DeviceAttributes>
                    <CallerAttributes>{caller_attrib}</CallerAttributes>
                    <Products>{products_str}</Products>
                </ProductsParameters>
            </parameters>
        </SyncUpdates>
    </s:Body>
</s:Envelope>"""

        resp = requests.post(
            self.CLIENT_URL,
            data=envelope.encode("utf-8"),
            headers=self.HEADERS,
            verify=False,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        out = html.unescape(resp.text)

        leaf_infos = [
            m
            for m in re.findall(r"<UpdateInfo>.*?</UpdateInfo>", out, re.S)
            if "<IsLeaf>true</IsLeaf>" in m
        ]
        updates_raw = re.findall(r"<Update>.*?</Update>", out, re.S)

        results = []
        for info_xml in leaf_infos:
            num_id_m = re.search(r"<ID>(\d+)</ID>", info_xml)
            uid_m = re.search(r'UpdateID="([^"]+)"', info_xml)
            rev_m = re.search(r'RevisionNumber="(\d+)"', info_xml)
            if not (num_id_m and uid_m):
                continue

            num_id = num_id_m.group(1)
            update_id = uid_m.group(1)
            rev = int(rev_m.group(1)) if rev_m else 1

            matching_updates = [u for u in updates_raw if f"<ID>{num_id}</ID>" in u]
            files_xml_blocks = [u for u in matching_updates if "<Files>" in u]
            if not files_xml_blocks:
                continue

            ver_m = re.search(r'ProductReleaseInstalled Name="([^"]+)" Version="([^"]+)"', info_xml)
            found_build = check_build.replace("10.0.", "")
            if ver_m:
                v_parts = ver_m.group(2).split(".")
                if len(v_parts) >= 4:
                    found_build = f"{v_parts[2]}.{v_parts[3]}"

            title_blocks = [u for u in matching_updates if "<Title>" in u]
            title = f"Windows 11 ({found_build})"
            if title_blocks:
                t_m = re.search(r"<Title>(.*?)</Title>", title_blocks[0])
                if t_m:
                    title = t_m.group(1)

            file_entries = re.findall(r"<File\s+.*?</File>", files_xml_blocks[0], re.S)
            sha1_map: Dict[str, Dict] = {}
            for fe in file_entries:
                d_m = re.search(r'Digest="([^"]+)"', fe)
                n_m = re.search(r'FileName="([^"]+)"', fe)
                s_m = re.search(r'Size="(\d+)"', fe)
                s256_m = re.search(
                    r'<AdditionalDigest[^>]*Algorithm="SHA256"[^>]*>(.*?)</AdditionalDigest>',
                    fe,
                )
                if not (d_m and n_m and s_m):
                    continue
                sha1_hex = base64.b64decode(d_m.group(1)).hex()
                sha256_hex = base64.b64decode(s256_m.group(1)).hex() if s256_m else None
                sha1_map[sha1_hex] = {
                    "name": n_m.group(1),
                    "size": int(s_m.group(1)),
                    "sha256": sha256_hex,
                }

            if sha1_map:
                results.append(
                    {
                        "update_id": update_id if rev == 1 else f"{update_id}_rev.{rev}",
                        "raw_update_id": update_id,
                        "revision": rev,
                        "title": title,
                        "build": found_build,
                        "check_build": check_build,
                        "arch": arch,
                        "ring": ring,
                        "sku": sku,
                        "files": sha1_map,
                    }
                )

        return results

    def get_download_urls(
        self,
        update_id: str,
        rev: int = 1,
        check_build: str = "10.0.26300.1",
        arch: str = "amd64",
        sku: int = 48,
        ring: str = "RETAIL",
    ) -> Dict[str, str]:
        """
        Call SOAP GetExtendedUpdateInfo2 on fe3cr.delivery.mp.microsoft.com/ClientWebService/client.asmx/secured
        to mint fresh signed download URLs mapped by lowercase SHA-1 hex digest.
        """
        if "_rev." in update_id:
            parts = update_id.split("_rev.")
            update_id = parts[0]
            rev = int(parts[1])

        now = datetime.datetime.now(datetime.timezone.utc)
        created = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        expires = (now + datetime.timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        device = self.generate_device_token()
        dev_attrib = self.compose_device_attributes(
            build=check_build, arch=arch, sku=sku, ring=ring
        )

        envelope = f"""<s:Envelope xmlns:a="http://www.w3.org/2005/08/addressing" xmlns:s="http://www.w3.org/2003/05/soap-envelope">
    <s:Header>
        <a:Action s:mustUnderstand="1">http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService/GetExtendedUpdateInfo2</a:Action>
        <a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>
        <a:To s:mustUnderstand="1">https://fe3.delivery.mp.microsoft.com/ClientWebService/client.asmx/secured</a:To>
        <o:Security s:mustUnderstand="1" xmlns:o="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <Timestamp xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                <Created>{created}</Created>
                <Expires>{expires}</Expires>
            </Timestamp>
            <wuws:WindowsUpdateTicketsToken wsu:id="ClientMSA" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd" xmlns:wuws="http://schemas.microsoft.com/msus/2014/10/WindowsUpdateAuthorization">
                <TicketType Name="MSA" Version="1.0" Policy="MBI_SSL">
                    <Device>{device}</Device>
                </TicketType>
            </wuws:WindowsUpdateTicketsToken>
        </o:Security>
    </s:Header>
    <s:Body>
        <GetExtendedUpdateInfo2 xmlns="http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService">
            <updateIDs>
                <UpdateIdentity>
                    <UpdateID>{update_id}</UpdateID>
                    <RevisionNumber>{rev}</RevisionNumber>
                </UpdateIdentity>
            </updateIDs>
            <infoTypes>
                <XmlUpdateFragmentType>FileUrl</XmlUpdateFragmentType>
                <XmlUpdateFragmentType>FileDecryption</XmlUpdateFragmentType>
                <XmlUpdateFragmentType>EsrpDecryptionInformation</XmlUpdateFragmentType>
                <XmlUpdateFragmentType>PiecesHashUrl</XmlUpdateFragmentType>
                <XmlUpdateFragmentType>BlockMapUrl</XmlUpdateFragmentType>
            </infoTypes>
            <deviceAttributes>{dev_attrib}</deviceAttributes>
        </GetExtendedUpdateInfo2>
    </s:Body>
</s:Envelope>"""

        resp = requests.post(
            self.SECURED_URL,
            data=envelope.encode("utf-8"),
            headers=self.HEADERS,
            verify=False,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        locs = re.findall(
            r"<FileLocation><FileDigest>(.*?)</FileDigest><Url>(.*?)</Url></FileLocation>",
            resp.text,
        )
        sha1_to_url: Dict[str, str] = {}
        for digest_b64, raw_url in locs:
            sha1_hex = base64.b64decode(digest_b64).hex()
            url = html.unescape(raw_url)
            # Prefer signed tlu.dl.delivery.mp.microsoft.com URLs over unsigned dl.delivery URLs
            if sha1_hex not in sha1_to_url or "P1=" in url:
                sha1_to_url[sha1_hex] = url
        return sha1_to_url


class UUPDownloader:
    """
    Resolves UUP build metadata and file sets for Tiny11 builds using
    Direct Microsoft Windows Update SOAP + Public UUP Dump API fallback.
    """

    UUPDUMP_API_BASE = "https://api.uupdump.net"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.wu_client = DirectWindowsUpdateClient(timeout=timeout)

    def find_latest_build(
        self,
        version: str = "26H2",
        build_filter: str = "",
        arch: str = "amd64",
    ) -> Dict:
        """
        Resolve the latest UUP update_id and build number for the requested Windows 11 version.
        Tries UUP Dump listid.php for fast lookup and falls back to direct WU SyncUpdates.
        """
        version_up = version.upper().strip() if version else "26H2"
        prefix, branch = VERSION_BUILD_MAP.get(version_up, ("26300", "ge_release"))
        if build_filter and "." in build_filter:
            prefix = build_filter.split(".")[0]

        # 1) Query UUP Dump listid.php for the latest matching Windows 11 x64 feature build
        try:
            logger.info(f"Looking up latest Windows 11 {version_up} ({arch}) build...")
            resp = requests.get(
                f"{self.UUPDUMP_API_BASE}/listid.php",
                params={"search": f"Windows 11 {prefix}", "sortByDate": "1"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            builds = data.get("response", {}).get("builds", {})
            build_items = (
                [builds[k] for k in sorted(builds.keys(), key=lambda k: int(k) if str(k).isdigit() else 0)]
                if isinstance(builds, dict)
                else (builds if isinstance(builds, list) else [])
            )
            for b in build_items:
                if not isinstance(b, dict):
                    continue
                title = b.get("title", "")
                b_arch = b.get("arch", "")
                b_build = str(b.get("build", ""))
                b_uuid = b.get("uuid", "")
                if b_arch != arch or not b_uuid:
                    continue
                # Skip standalone .NET / Cumulative Update-only entries that lack full UUP editions
                if any(
                    skip in title
                    for skip in (".NET Framework", "Cumulative Update for", "Preview Update for")
                ):
                    continue
                if build_filter and b_build != build_filter:
                    continue
                if b_build.startswith(prefix) or version_up.lower() in title.lower():
                    logger.info(f"Resolved UUP build: {title} ({b_build}) -> {b_uuid}")
                    return {
                        "update_id": b_uuid,
                        "build_number": b_build,
                        "title": title,
                        "version": version_up,
                        "arch": arch,
                    }
        except Exception as exc:
            logger.warning(f"UUP Dump listid lookup failed ({exc}), trying direct Windows Update SOAP...")

        # 2) Direct Microsoft Windows Update SOAP SyncUpdates fallback
        seed_build = build_filter if build_filter else f"{prefix}.1"
        for ring in ("RETAIL", "RP", "WIS", "WIF"):
            try:
                updates = self.wu_client.sync_updates(
                    check_build=f"10.0.{seed_build}",
                    arch=arch,
                    ring=ring,
                    branch=branch,
                    flags=("thisonly",) if "." in seed_build and not seed_build.endswith(".1") else (),
                )
                for upd in updates:
                    if ".NET" not in upd["title"]:
                        logger.info(
                            f"Resolved via Direct WU SOAP ({ring}): {upd['title']} ({upd['build']}) -> {upd['update_id']}"
                        )
                        return {
                            "update_id": upd["update_id"],
                            "build_number": upd["build"],
                            "title": upd["title"],
                            "version": version_up,
                            "arch": arch,
                            "wu_metadata": upd,
                        }
            except Exception as exc:
                logger.debug(f"Direct WU SyncUpdates ({ring}) failed: {exc}")

        raise RuntimeError(
            f"Could not resolve a UUP update ID for Windows 11 {version_up} (build='{build_filter}')."
        )

    @staticmethod
    def _extract_cab_file(cab_path: Path, dest_dir: Path) -> None:
        """Extract a CAB archive using Windows expand.exe, 7z, or cabextract."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            expand_exe = shutil.which("expand.exe") or os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "expand.exe"
            )
            if os.path.exists(expand_exe):
                res = subprocess.run(
                    [expand_exe, str(cab_path), "-F:*", str(dest_dir)],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0 and any(dest_dir.iterdir()):
                    return
                # Fallback for single-file CABs where expand.exe expects an explicit output file path
                out_file = dest_dir / cab_path.stem
                res2 = subprocess.run(
                    [expand_exe, str(cab_path), str(out_file)],
                    capture_output=True,
                    text=True,
                )
                if res2.returncode == 0 and out_file.exists():
                    return

        for tool, args in (
            ("7z", ["x", "-y", f"-o{dest_dir}", str(cab_path)]),
            ("7za", ["x", "-y", f"-o{dest_dir}", str(cab_path)]),
            ("cabextract", ["-d", str(dest_dir), str(cab_path)]),
        ):
            exe = shutil.which(tool)
            if exe:
                res = subprocess.run([exe] + args, capture_output=True, text=True)
                if res.returncode == 0:
                    return

        raise RuntimeError(f"Failed to extract CAB file: {cab_path}")

    def resolve_files_direct_wu(
        self,
        update_id: str,
        lang: str,
        uup_edition: str,
        check_build: str = "26300.1",
        wu_metadata: Optional[Dict] = None,
    ) -> Dict:
        """
        Resolve edition files and fresh Microsoft CDN URLs directly via Microsoft's
        ClientWebService SOAP endpoints + AggregatedMetadata CompDB XML parsing
        (pure Python port of standalone/shared/genpack.php + api/get.php).
        """
        logger.info(
            f"Resolving UUP files directly from Microsoft Windows Update for {update_id} ({lang}, {uup_edition})..."
        )
        if not wu_metadata or "files" not in wu_metadata:
            try:
                info_resp = requests.get(
                    f"{self.UUPDUMP_API_BASE}/updateinfo.php",
                    params={"id": update_id},
                    timeout=self.timeout,
                )
                info_resp.raise_for_status()
                info_data = info_resp.json().get("response", {}).get("info", {})
                if info_data and "files" in info_data:
                    wu_metadata = {
                        "update_id": update_id,
                        "title": info_data.get("title", f"Windows 11 ({check_build})"),
                        "build": info_data.get("build", check_build),
                        "check_build": info_data.get("checkBuild", f"10.0.{check_build}"),
                        "arch": info_data.get("arch", "amd64"),
                        "ring": info_data.get("ring", "RETAIL"),
                        "sku": int(info_data.get("sku", 48)),
                        "files": {
                            sha1: {
                                "name": f["name"],
                                "size": int(f["size"]),
                                "sha256": f.get("sha256"),
                            }
                            for sha1, f in info_data["files"].items()
                        },
                    }
            except Exception as info_err:
                logger.debug(f"updateinfo.php lookup failed ({info_err}), querying WU SyncUpdates...")

            if not wu_metadata or "files" not in wu_metadata:
                for ring in ("RETAIL", "WIF", "WIS", "RP"):
                    updates = self.wu_client.sync_updates(
                        check_build=check_build, arch="amd64", ring=ring, flags=("thisonly",)
                    )
                    matched = [
                        u for u in updates if u["update_id"] == update_id or u["raw_update_id"] == update_id
                    ]
                    if matched:
                        wu_metadata = matched[0]
                        break

            if not wu_metadata or "files" not in wu_metadata:
                raise RuntimeError(f"Could not retrieve file metadata for {update_id}")

        sha1_to_url = self.wu_client.get_download_urls(
            update_id=update_id,
            check_build=wu_metadata.get("check_build", f"10.0.{check_build}"),
            arch=wu_metadata.get("arch", "amd64"),
            sku=int(wu_metadata.get("sku", 48)),
            ring=wu_metadata.get("ring", "RETAIL"),
        )

        # Build clean filename -> file info dictionary (ported from uupGetOnlineFiles in api/get.php)
        all_files: Dict[str, Dict] = {}
        sha256_to_name: Dict[str, str] = {}
        for sha1_hex, meta in wu_metadata["files"].items():
            url = sha1_to_url.get(sha1_hex)
            if not url:
                continue
            clean_name = clean_uup_filename(meta["name"], sha256_capable= bool(meta.get("sha256")))
            size = int(meta["size"])
            if clean_name not in all_files or size > all_files[clean_name]["size"]:
                all_files[clean_name] = {
                    "sha1": sha1_hex,
                    "sha256": meta.get("sha256"),
                    "size": size,
                    "url": url,
                }
                if meta.get("sha256"):
                    sha256_to_name[meta["sha256"].lower()] = clean_name

        # Find and unpack AggregatedMetadata.cab to parse DesktopTargetCompDB_<edition>_<lang>.xml
        agg_candidates = sorted(
            [k for k in all_files if "aggregatedmetadata" in k.lower() and k.lower().endswith(".cab")]
        )
        if not agg_candidates:
            raise RuntimeError("No AggregatedMetadata.cab found in update file list")

        required_sha256s = set()
        with tempfile.TemporaryDirectory(prefix="uup_meta_") as tmp_str:
            tmp_dir = Path(tmp_str)
            agg_name = agg_candidates[0]
            agg_path = tmp_dir / agg_name
            r_agg = requests.get(all_files[agg_name]["url"], timeout=self.timeout)
            r_agg.raise_for_status()
            agg_path.write_bytes(r_agg.content)

            extract_dir = tmp_dir / "extracted"
            self._extract_cab_file(agg_path, extract_dir)

            # Match DesktopTargetCompDB_<edition>_<lang>.xml(.cab)
            target_pattern = re.compile(
                rf"DesktopTargetCompDB_{re.escape(uup_edition)}_{re.escape(lang)}\.xml",
                re.IGNORECASE,
            )
            matched_compdbs = [
                p for p in extract_dir.rglob("*") if p.is_file() and target_pattern.search(p.name)
            ]
            if not matched_compdbs:
                raise RuntimeError(
                    f"CompDB for edition '{uup_edition}' and language '{lang}' not found in {agg_name}"
                )

            compdb_file = matched_compdbs[0]
            if compdb_file.suffix.lower() == ".cab":
                xml_dir = tmp_dir / "compdb_xml"
                self._extract_cab_file(compdb_file, xml_dir)
                xml_files = [p for p in xml_dir.rglob("*") if p.is_file()]
                if not xml_files:
                    raise RuntimeError(f"No XML inside {compdb_file.name}")
                compdb_file = xml_files[0]

            tree = ET.parse(compdb_file)
            root = tree.getroot()
            for elem in root.iter():
                if elem.tag.endswith("PayloadItem"):
                    phash = elem.attrib.get("PayloadHash")
                    if phash:
                        required_sha256s.add(base64.b64decode(phash).hex().lower())

        selected_files: Dict[str, Dict] = {}
        for s256 in required_sha256s:
            fname = sha256_to_name.get(s256)
            if fname and fname in all_files:
                selected_files[fname] = all_files[fname]

        # Add servicing / cumulative update files (matching api/get.php lines 180-312)
        updates_re = re.compile(r"^(Windows(10|11)\.0-KB.*|SSU-.*)\.(cab|msu|psf)$", re.IGNORECASE)
        for fname, finfo in all_files.items():
            low = fname.lower()
            if any(x in low for x in ("_diffs_", "_forward_compdb_", ".cbsu.cab", "baseless", "-express")):
                continue
            if updates_re.match(fname):
                selected_files[fname] = finfo

        return {
            "updateName": wu_metadata.get("title", f"Windows 11 ({check_build})"),
            "arch": wu_metadata.get("arch", "amd64"),
            "build": wu_metadata.get("build", check_build),
            "files": selected_files,
        }

    def resolve_files_uupdump_api(
        self, update_id: str, lang: str, uup_edition: str
    ) -> Dict:
        """Fallback: resolve edition file list via https://api.uupdump.net/get.php."""
        logger.info(
            f"Fetching UUP file list from api.uupdump.net for {update_id} (lang={lang}, edition={uup_edition.lower()})..."
        )
        resp = requests.get(
            f"{self.UUPDUMP_API_BASE}/get.php",
            params={
                "id": update_id,
                "lang": lang.lower(),
                "edition": uup_edition.lower(),
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json().get("response", {})
        if "error" in payload:
            raise RuntimeError(f"UUP Dump API error: {payload['error']}")
        if not payload.get("files"):
            raise RuntimeError("UUP Dump API returned an empty file list")
        return payload

    @staticmethod
    def filter_files_for_tiny11(
        files: Dict[str, Dict], include_lcu: bool = True
    ) -> Tuple[Dict[str, Dict], List[str]]:
        """
        Filter out files that Tiny11 never needs to download:
          1. Edge.wim (~172 MB) - Tiny11 deletes Edge and wimlib-imagex never references Edge.wim.
          2. *AggregatedMetadata*.cab & *DesktopDeployment*.cab - only used for UUP metadata/legacy converter.
          3. When include_lcu=False (Core / Core-Gaming / Nano builds): skip heavy *.msu and *-NDP*.cab
             cumulative update payloads (~4.75 GB), while keeping small KB CABs (<250 MB) such as
             Enablement Packages (EKB) and Setup/SafeOS Dynamic Updates.
        """
        kept: Dict[str, Dict] = {}
        skipped: List[str] = []

        for name, info in files.items():
            low = name.lower()
            size_bytes = int(info.get("size", 0))

            # Always skip Edge.wim and UUP metadata-only CABs
            if (
                low == "edge.wim"
                or "aggregatedmetadata" in low
                or "desktopdeployment" in low
            ):
                skipped.append(f"{name} ({size_bytes / (1024 * 1024):.1f} MB - unnecessary metadata/Edge)")
                continue

            if not include_lcu:
                is_msu = low.endswith(".msu")
                is_ndp_kb = "kb" in low and "ndp" in low and low.endswith(".cab")
                is_huge_kb_cab = (
                    ("kb" in low or low.startswith("ssu-"))
                    and low.endswith(".cab")
                    and size_bytes > 250 * 1024 * 1024
                )
                if is_msu or is_ndp_kb or is_huge_kb_cab:
                    skipped.append(
                        f"{name} ({size_bytes / (1024 * 1024):.1f} MB - heavy LCU skipped for non-serviceable build)"
                    )
                    continue

            kept[name] = info

        return kept, skipped

    def resolve_and_prepare(
        self,
        version: str = "26H2",
        build_filter: str = "",
        update_id: str = "",
        language: str = "English (United States)",
        image_index: int = 1,
        include_lcu: bool = True,
        output_dir: Path = Path("UUPs"),
        aria2_out: Path = Path("uup_aria2.txt"),
        metadata_out: Path = Path("uup_metadata.json"),
    ) -> Dict:
        """
        Full pipeline:
          1. Normalize language & edition from Tiny11 inputs.
          2. Resolve Update ID + build number if not supplied.
          3. Resolve the exact file list via Direct Microsoft WU SOAP (with fallback to api.uupdump.net,
             or vice versa).
          4. Filter out bloatware/Edge/unneeded LCU packages.
          5. Write the aria2c download script and uup_metadata.json.
        """
        lang_code = normalize_language(language)
        ed_info = resolve_edition_info(image_index)
        uup_edition = ed_info["uup_edition"] or "CORE"

        wu_meta = None
        resolved_build = build_filter
        resolved_title = f"Windows 11 {version}"

        if not update_id:
            found = self.find_latest_build(version=version, build_filter=build_filter)
            update_id = found["update_id"]
            resolved_build = found["build_number"]
            resolved_title = found["title"]
            wu_meta = found.get("wu_metadata")

        # Try Direct WU SOAP first; fallback to public UUP Dump API if needed
        raw_result = None
        source_used = "direct_microsoft_wu"
        try:
            raw_result = self.resolve_files_direct_wu(
                update_id=update_id,
                lang=lang_code,
                uup_edition=uup_edition,
                check_build=resolved_build or VERSION_BUILD_MAP.get(version.upper(), ("26300",))[0] + ".1",
                wu_metadata=wu_meta,
            )
        except Exception as direct_err:
            logger.warning(
                f"Direct WU resolution encountered an issue ({direct_err}); falling back to api.uupdump.net..."
            )
            source_used = "uupdump_api_fallback"
            raw_result = self.resolve_files_uupdump_api(
                update_id=update_id,
                lang=lang_code,
                uup_edition=uup_edition,
            )

        if not resolved_build and raw_result.get("build"):
            resolved_build = str(raw_result["build"])
        if raw_result.get("updateName"):
            resolved_title = raw_result["updateName"]

        filtered_files, skipped_files = self.filter_files_for_tiny11(
            raw_result["files"], include_lcu=include_lcu
        )

        output_dir.mkdir(parents=True, exist_ok=True)

        # Sort files by size ascending (same as standalone/get.php)
        sorted_names = sorted(
            filtered_files.keys(),
            key=lambda k: int(filtered_files[k].get("size", 0)),
        )

        aria2_lines: List[str] = []
        total_bytes = 0
        for fname in sorted_names:
            finfo = filtered_files[fname]
            total_bytes += int(finfo.get("size", 0))
            aria2_lines.append(finfo["url"])
            aria2_lines.append(f"  out={fname}")
            if finfo.get("sha1"):
                aria2_lines.append(f"  checksum=sha-1={finfo['sha1']}")
            aria2_lines.append("")

        aria2_out.parent.mkdir(parents=True, exist_ok=True)
        aria2_out.write_text("\n".join(aria2_lines), encoding="utf-8")

        metadata = {
            "update_id": update_id,
            "windows_version": version,
            "windows_build": resolved_build,
            "update_title": resolved_title,
            "language_input": language,
            "language_code": lang_code,
            "image_index": image_index,
            "uup_edition": uup_edition,
            "virtual_edition": ed_info["virtual_edition"],
            "edition_name": ed_info["edition_name"],
            "image_name": ed_info["image_name"],
            "image_flag": ed_info["image_flag"],
            "include_lcu": include_lcu,
            "resolution_source": source_used,
            "files_count": len(filtered_files),
            "skipped_count": len(skipped_files),
            "skipped_files": skipped_files,
            "total_size_mb": round(total_bytes / (1024 * 1024), 2),
            "total_size_gb": round(total_bytes / (1024 ** 3), 2),
            "output_dir": str(output_dir.resolve()),
            "aria2_script": str(aria2_out.resolve()),
        }

        metadata_out.parent.mkdir(parents=True, exist_ok=True)
        metadata_out.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return metadata


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve Windows 11 UUP files and generate an aria2c download script for Tiny11."
    )
    parser.add_argument("--version", default="26H2", help="Windows 11 version (e.g., 24H2, 25H2, 26H2)")
    parser.add_argument("--build", default="", help="Optional specific build number (e.g., 26300.9725)")
    parser.add_argument("--update-id", default="", help="Optional specific UUP Update UUID")
    parser.add_argument(
        "--language",
        default="English (United States)",
        help="Language name (e.g., 'English (United States)', 'Ukrainian') or code ('en-us')",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=1,
        choices=[1, 4, 6, 7],
        help="Tiny11 edition index (1=Home, 4=Education, 6=Pro, 7=Pro N)",
    )
    lcu_group = parser.add_mutually_exclusive_group()
    lcu_group.add_argument(
        "--include-lcu",
        dest="include_lcu",
        action="store_true",
        default=True,
        help="Include heavy Cumulative Update (.msu) packages (default for Standard builds)",
    )
    lcu_group.add_argument(
        "--skip-lcu",
        dest="include_lcu",
        action="store_false",
        help="Skip heavy Cumulative Update (.msu) packages (recommended for Core/Gaming/Nano builds)",
    )
    parser.add_argument("--output-dir", default="UUPs", help="Directory where UUP files will be downloaded")
    parser.add_argument("--aria2-out", default="uup_aria2.txt", help="Output path for aria2c input script")
    parser.add_argument(
        "--metadata-out", default="uup_metadata.json", help="Output path for resolved UUP metadata JSON"
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Immediately invoke aria2c to download all resolved UUP files into --output-dir",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] %(message)s")

    # Strip 'uup:' prefix if passed via workflow windows_iso_url input
    update_id = args.update_id.strip()
    if update_id.lower().startswith("uup:"):
        update_id = update_id[4:].strip()

    downloader = UUPDownloader()
    meta = downloader.resolve_and_prepare(
        version=args.version,
        build_filter=args.build,
        update_id=update_id,
        language=args.language,
        image_index=args.index,
        include_lcu=args.include_lcu,
        output_dir=Path(args.output_dir),
        aria2_out=Path(args.aria2_out),
        metadata_out=Path(args.metadata_out),
    )

    logger.info(
        f"Resolved {meta['files_count']} UUP files ({meta['total_size_gb']} GB) via {meta['resolution_source']} "
        f"[skipped {meta['skipped_count']} unneeded/bloatware package(s)]"
    )

    if args.download:
        aria2_exe = shutil.which("aria2c")
        if not aria2_exe:
            raise RuntimeError("aria2c is not installed or not in PATH")
        cmd = [
            aria2_exe,
            "--no-conf",
            "--console-log-level=warn",
            "--summary-interval=30",
            "--max-connection-per-server=16",
            "--split=16",
            "--max-concurrent-downloads=8",
            "--min-split-size=1M",
            "--max-tries=5",
            "--retry-wait=3",
            "--timeout=60",
            "--connect-timeout=30",
            "--continue=true",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--file-allocation=none",
            "--check-integrity=true",
            "-d",
            str(Path(args.output_dir)),
            "-i",
            str(Path(args.aria2_out)),
        ]
        logger.info(f"Starting aria2c parallel download into {args.output_dir}...")
        subprocess.run(cmd, check=True)
        logger.info("UUP download completed successfully.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
