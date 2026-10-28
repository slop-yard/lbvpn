#!/usr/bin/env python3
"""Generate a self-contained Docker Compose bundle from the command line.

    N x gluetun (one Proton exit per region)  ->  nginx-stream LB  ->  optionally SearXNG

Usage:
    python generate.py --regions fr,ch --lb-mode rr OUTPUT_DIR
    python generate.py --regions fr,ch -f .env --include-stub OUTPUT_DIR

No env file is required. `-f` is optional: when given, its values are checked
and copied in as `.env` (and, with --inline, baked straight into the compose).
Either way the output folder gets a `.env.example` listing every variable the
stack expects.
"""
from __future__ import annotations

import argparse
import re
import secrets
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STUB = HERE / "stub"

TOKEN = re.compile(r"@@[A-Z_]+@@")

# ISO-3166 alpha-2 -> name used by the server filters (plus a couple of
# common aliases). Convenience only; <REGION>_COUNTRY always wins.
COUNTRY_NAMES = {
    "ad": "Andorra", "ae": "United Arab Emirates", "al": "Albania",
    "ar": "Argentina", "at": "Austria", "au": "Australia", "ba": "Bosnia and Herzegovina",
    "be": "Belgium", "bg": "Bulgaria", "br": "Brazil", "ca": "Canada",
    "ch": "Switzerland", "cl": "Chile", "co": "Colombia", "cr": "Costa Rica",
    "cy": "Cyprus", "cz": "Czechia", "de": "Germany", "dk": "Denmark",
    "ee": "Estonia", "eg": "Egypt", "en": "United Kingdom", "es": "Spain",
    "fi": "Finland", "fr": "France", "gb": "United Kingdom", "ge": "Georgia",
    "gr": "Greece", "hk": "Hong Kong", "hr": "Croatia", "hu": "Hungary",
    "id": "Indonesia", "ie": "Ireland", "il": "Israel", "in": "India",
    "is": "Iceland", "it": "Italy", "jp": "Japan", "kr": "South Korea",
    "kz": "Kazakhstan", "lt": "Lithuania", "lu": "Luxembourg", "lv": "Latvia",
    "md": "Moldova", "mx": "Mexico", "my": "Malaysia", "ng": "Nigeria",
    "nl": "Netherlands", "no": "Norway", "nz": "New Zealand", "pe": "Peru",
    "ph": "Philippines", "pl": "Poland", "pt": "Portugal", "ro": "Romania",
    "rs": "Serbia", "se": "Sweden", "sg": "Singapore", "si": "Slovenia",
    "sk": "Slovakia", "th": "Thailand", "tr": "Turkey", "tw": "Taiwan",
    "ua": "Ukraine", "uk": "United Kingdom", "us": "United States",
    "vn": "Vietnam", "za": "South Africa",
}

# <REGION>_<SUFFIX> -> gluetun env var. Anything else under the region prefix
# found in an env file is passed through with the suffix used verbatim.
MAPPED_SUFFIXES = {
    "WG_KEY": "WIREGUARD_PRIVATE_KEY",
    "WG_ADDRESS": "WIREGUARD_ADDRESSES",
    "COUNTRY": "SERVER_COUNTRIES",
    "CITY": "SERVER_CITIES",
}


def fail(message: str) -> "None":
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_env(path: Path) -> dict:
    if not path.is_file():
        fail(f"env file not found: {path}")
    values: dict = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def render(text: str, **tokens) -> str:
    for key, value in tokens.items():
        text = text.replace(f"@@{key}@@", str(value))
    leftover = sorted(set(TOKEN.findall(text)))
    if leftover:
        fail(f"unsubstituted placeholders: {', '.join(leftover)}")
    return text


def yq(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def template(name: str) -> str:
    path = TEMPLATES / name
    if not path.is_file():
        fail(f"missing template: {path}")
    return path.read_text(encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate.py",
        description="Generate a docker compose bundle (N exits + LB + optional SearXNG).",
    )
    parser.add_argument("--regions", metavar="fr,ch",
                        help="comma separated region codes (required unless the env file sets REGIONS)")
    parser.add_argument("-f", "--env-file", metavar="ENV_FILE",
                        help="optional env file to read values from")
    parser.add_argument("--lb-mode", default="rr", metavar="MODE",
                        help="rr | <region> | <host:port> (default: rr)")
    parser.add_argument("--no-searchxng", action="store_true",
                        help="do not bundle SearXNG")
    parser.add_argument("--include-stub", action="store_true",
                        help="copy the stub proxy in so you can test without VPN keys")
    parser.add_argument("--inline", action="store_true",
                        help="bake values from the env file into compose.yaml instead of ${VAR} refs")
    parser.add_argument("out", metavar="OUTPUT_DIR", help="folder to write into")
    args = parser.parse_args(argv)

    env = read_env(Path(args.env_file)) if args.env_file else {}
    if args.inline and not args.env_file:
        fail("--inline needs an env file (-f)")

    regions_field = args.regions or env.get("REGIONS", "")
    regions = [r.strip().lower() for r in regions_field.split(",") if r.strip()]
    if not regions:
        fail("no regions: pass --regions fr,ch (or set REGIONS= in an env file)")
    duplicates = sorted({r for r in regions if regions.count(r) > 1})
    if duplicates:
        fail(f"duplicate region(s): {', '.join(duplicates)}")

    if args.env_file:
        missing = [f"{r.upper()}_{s}" for r in regions for s in ("WG_KEY", "WG_ADDRESS")
                   if not env.get(f"{r.upper()}_{s}")]
        if missing:
            fail(f"{args.env_file}: missing required key(s): {', '.join(missing)}")

    # --- region services ----------------------------------------------------
    region_template = template("region-service.yaml")
    fragments = []
    section_blocks = []
    readme_lines = []
    for region in regions:
        up = region.upper()
        country = env.get(f"{up}_COUNTRY") or COUNTRY_NAMES.get(region)

        if args.inline:
            key, address = env[f"{up}_WG_KEY"], env[f"{up}_WG_ADDRESS"]
            if not country:
                fail(f"region '{region}' has no country name; set {up}_COUNTRY=<country>")
            country_token = country
        else:
            key = f"${{{up}_WG_KEY}}"
            address = f"${{{up}_WG_ADDRESS}}"
            if country:
                country_token = f"${{{up}_COUNTRY:-{country}}}"
            else:
                country_token = f"${{{up}_COUNTRY}}"
                print(f"warning: unknown region code '{region}'; "
                      f"set {up}_COUNTRY in .env", file=sys.stderr)

        city_line = ""
        if env.get(f"{up}_CITY"):
            city = f"${{{up}_CITY}}" if not args.inline else env[f"{up}_CITY"]
            city_line = f"\n      SERVER_CITIES: {yq(city)}"

        extra_lines = ""
        prefix = f"{up}_"
        for name, value in env.items():
            if name.startswith(prefix) and name[len(prefix):] not in MAPPED_SUFFIXES:
                shown = value if args.inline else f"${{{up}_{name[len(prefix):]}}}"
                extra_lines += f"\n      {name[len(prefix):]}: {yq(shown)}"

        fragments.append(render(
            region_template, REGION=region, WG_KEY=key, WG_ADDRESS=address,
            COUNTRY=country_token, CITY=city_line, EXTRA=extra_lines,
        ).rstrip("\n"))

        section_blocks.append(
            f"# --- {region} : {country or 'set ' + up + '_COUNTRY'} ---\n"
            f"{up}_WG_KEY=\n{up}_WG_ADDRESS=\n"
            f"# {up}_COUNTRY={country or ''}\n# {up}_CITY=\n"
        )
        readme_lines.append(f"  {region:>3} -> vpn-{region:<10} {country or '(set ' + up + '_COUNTRY)'}")

    # compose requires ^[a-z0-9][a-z0-9_-]*$
    project = re.sub(r"[^a-z0-9_-]+", "-", Path(args.out).resolve().name.lower()).strip("-_") or "mitmvpn"

    # --- SearXNG / LB -------------------------------------------------------
    searxng_block = ""
    searxng_env = ""
    searxng_files = ""
    searxng_check = ""
    searxng_note = ""
    if not args.no_searchxng:
        port = env.get("SEARXNG_PORT", "8080")
        searxng_block = "\n" + render(template("searxng-service.yaml"), SEARXNG_PORT=port)
        searxng_env = "SEARXNG_PORT=8080\n# SEARXNG_SECRET=\n"
        searxng_files = "  configs/searxng.yaml        SearXNG settings (points at the LB)\n"
        searxng_check = f"\n  # SearXNG UI\n  http://<host>:{port}\n"
        searxng_note = " and SearXNG"
    stub_files = "  stub/                       stub proxy for testing without VPN keys\n" if args.include_stub else ""

    compose = render(
        template("compose.yaml"),
        PROJECT=project,
        REGION_SERVICES="\n\n".join(fragments),
        LB_MODE=args.lb_mode,
        VPN_HOSTS=" ".join(f"vpn-{r}:8888" for r in regions),
        LB_PORT=env.get("LB_PORT", "8890"),
        LB_DEPENDS="".join(f"\n      - vpn-{r}" for r in regions),
        SEARXNG=searxng_block,
    )

    env_example = render(
        template("env.example"),
        REGIONS=",".join(regions),
        REGION_SECTIONS="\n".join(section_blocks),
        LB_MODE=args.lb_mode,
        LB_PORT=env.get("LB_PORT", "8890"),
        SEARXNG_ENV=searxng_env,
    )

    readme = render(
        template("README.txt"),
        PROJECT=project,
        UNDERLINE="=" * len(project),
        REGION_COUNT=len(regions),
        REGIONS=", ".join(regions),
        SEARXNG_NOTE=searxng_note,
        SEARXNG_FILES=searxng_files,
        STUB_FILES=stub_files,
        LB_PORT=env.get("LB_PORT", "8890"),
        SEARXNG_CHECK=searxng_check,
        FIRST_REGION=regions[0],
        REGION_LINES="\n".join(readme_lines),
    )

    # --- write --------------------------------------------------------------
    out = Path(args.out)
    configs = out / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    written = []

    def write(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)

    write(out / "compose.yaml", compose)
    write(out / ".env.example", env_example)
    write(out / "README.txt", readme)
    write(configs / "lb-entrypoint.sh", template("lb-entrypoint.sh"))
    if not args.no_searchxng:
        secret = env.get("SEARXNG_SECRET") or secrets.token_hex(16)
        write(configs / "searxng.yaml", render(template("searxng.yaml"), SECRET=secret))

    if args.env_file:
        shutil.copyfile(args.env_file, out / ".env")
        written.append(out / ".env")

    if args.include_stub:
        if not (STUB / "proxy.py").is_file():
            fail(f"stub sources not found at {STUB}")
        shutil.copytree(STUB, out / "stub", dirs_exist_ok=True)
        written.extend(sorted((out / "stub").iterdir()))

    print(f"\n{len(regions)} region(s) -> {out.resolve()}")
    for path in written:
        print(f"  {path.relative_to(out)}")
    if args.env_file:
        print(f"\nnext:\n  cd {out} && docker compose up -d")
    else:
        print(f"\nnext:\n  cd {out} && cp .env.example .env && docker compose up -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
