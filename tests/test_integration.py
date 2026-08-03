"""Integration tests against the live Sashiko instances (no network mocking).

Two real series are used:

- macb-context v6: reviewed by both the non-net and net instances
  (https://sashiko.dev/#/patchset/20260731-macb-context-v6-0-49d5a1439d48%40bootlin.com)
- mathieu-wdt-clock v2: reviewed by the non-net instance only
  (https://sashiko.dev/#/patchset/20260727-mathieu-wdt-clock-theo-v2-0-c048a6394436%40bootlin.com)

Tests run the CLI as a subprocess with --output-dir into a temp dir, then
inspect the generated .eml files. Date and Message-ID headers are
non-deterministic, so assertions are structural.
"""

import email
import os
import subprocess
import sys
from email import policy

MACB_URL = "https://sashiko.dev/#/patchset/20260731-macb-context-v6-0-49d5a1439d48%40bootlin.com"
MACB_FILES = {
    "07-net-macb-introduce-macb_context-struct-for-buffer-management.eml",
    "11-net-macb-change-function-signatures-to-take-contexts.eml",
    "13-net-macb-move-printk-calls-out-of-bp-lock-critical-section.eml",
    "14-net-macb-read-ISR-inside-bp-lock-critical-section.eml",
    "15-net-macb-use-context-swapping-in-.set_ringparam.eml",
    "16-net-macb-use-context-swapping-in-.ndo_change_mtu.eml",
}

WDT_URL = (
    "https://sashiko.dev/#/patchset/20260727-mathieu-wdt-clock-theo-v2-0-c048a6394436%40bootlin.com"
)
WDT_FILES = {
    "01-clk-ti-mux-resolve-parent-clocks-by-DT-index-not-by-name.eml",
    "02-clk-ti-composite-resolve-parent-clocks-by-DT-index-not-by-name.eml",
}

# Deterministic identity regardless of the machine's git config: ignore
# global/system config files and set user.name/user.email via the env.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "user.name",
    "GIT_CONFIG_VALUE_0": "Test User",
    "GIT_CONFIG_KEY_1": "user.email",
    "GIT_CONFIG_VALUE_1": "test@example.com",
}


def run_tool(args, *, env=None):
    return subprocess.run(
        [sys.executable, "-m", "sashiko_drafts", *args],
        env={**os.environ, **GIT_ENV, **(env or {})},
        capture_output=True,
        text=True,
    )


def read_eml(path):
    return email.message_from_bytes(path.read_bytes(), policy=policy.default)


def text(m):
    """Decoded message body with CRLF (SMTP policy) normalized to LF."""
    return m.get_content().replace("\r\n", "\n")


def test_macb_series_both_instances(tmp_path):
    out = tmp_path / "out"
    p = run_tool(["--msgid", MACB_URL, "--output-dir", str(out), "--signature", "test-sig"])
    assert p.returncode == 0, p.stderr
    assert {f.name for f in out.iterdir()} == MACB_FILES

    # A patch reviewed by both instances: non-net section first, then net.
    both = read_eml(out / "14-net-macb-read-ISR-inside-bp-lock-critical-section.eml")
    assert both["In-Reply-To"] == "<20260731-macb-context-v6-14-49d5a1439d48@bootlin.com>"
    assert both["References"] == both["In-Reply-To"]
    assert both["Subject"].startswith("Re: [PATCH net-next v6 14/16]")
    assert "Test User" in both["From"]
    assert "netdev@vger.kernel.org" in both["Cc"]
    btext = text(both)
    assert btext.index("Replying to non-net sashiko") < btext.index("Replying to net sashiko")
    assert btext.endswith("-- \ntest-sig\n\n")
    # The quoted review is one extra "> " level deep.
    assert "> " in btext

    # A patch reviewed by the net instance only: single section, net label.
    net_only = read_eml(out / "07-net-macb-introduce-macb_context-struct-for-buffer-management.eml")
    assert net_only["In-Reply-To"] == "<20260731-macb-context-v6-7-49d5a1439d48@bootlin.com>"
    assert "Replying to net sashiko" in text(net_only)
    assert "Replying to non-net sashiko" not in text(net_only)


def test_wdt_series_non_net_only(tmp_path):
    out = tmp_path / "out"
    sig_file = tmp_path / "sig.txt"
    sig_file.write_text("wdt-sig\n")
    p = run_tool(["--msgid", WDT_URL, "--output-dir", str(out), "--signature-file", str(sig_file)])
    assert p.returncode == 0, p.stderr
    assert {f.name for f in out.iterdir()} == WDT_FILES
    assert "(series not found)" in p.stderr  # net instance has no such series

    m = read_eml(out / "01-clk-ti-mux-resolve-parent-clocks-by-DT-index-not-by-name.eml")
    assert m["Subject"].startswith("Re: [PATCH v2 1/2]")
    body = text(m)
    assert "Replying to sashiko" in body  # single instance -> unqualified label
    assert "Replying to non-net sashiko" not in body
    assert body.endswith("-- \nwdt-sig\n\n")


def test_no_signature(tmp_path):
    out = tmp_path / "out"
    p = run_tool(["--msgid", WDT_URL, "--output-dir", str(out), "--no-signature"])
    assert p.returncode == 0, p.stderr
    body = text(read_eml(out / "01-clk-ti-mux-resolve-parent-clocks-by-DT-index-not-by-name.eml"))
    assert body.rstrip("\n").endswith("Thanks,")
    assert not body.rstrip("\n").endswith("-- ")  # no signature separator line


def test_default_signature_is_none(tmp_path):
    out = tmp_path / "out"
    p = run_tool(["--msgid", WDT_URL, "--output-dir", str(out)])
    assert p.returncode == 0, p.stderr
    body = text(read_eml(out / "01-clk-ti-mux-resolve-parent-clocks-by-DT-index-not-by-name.eml"))
    assert body.rstrip("\n").endswith("Thanks,")  # no signature block


def test_signature_from_git_config(tmp_path):
    out = tmp_path / "out"
    p = run_tool(
        ["--msgid", WDT_URL, "--output-dir", str(out)],
        env={
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_2": "format.signature",
            "GIT_CONFIG_VALUE_2": "config-sig",
        },
    )
    assert p.returncode == 0, p.stderr
    body = text(read_eml(out / "01-clk-ti-mux-resolve-parent-clocks-by-DT-index-not-by-name.eml"))
    assert body.endswith("-- \nconfig-sig\n\n")


def test_missing_git_is_reported(tmp_path):
    p = run_tool(
        ["--msgid", MACB_URL, "--output-dir", str(tmp_path / "out")],
        env={"PATH": "/nonexistent"},
    )
    assert p.returncode != 0
    assert "git" in p.stderr and "not found in PATH" in p.stderr
