#!/usr/bin/env python3
"""lbvpn - generate a Docker Compose bundle: N WireGuard exits (custom provider)
behind an nginx load balancer, plus optional consumer modules.

    python generate.py --scan ./my-proton-confs --with-mitmproxy --with-debug ./vpn

Exits come from WireGuard `.conf` files found by --scan. One file = one exit.
The id is taken from an `XX#NN` token found in the file (e.g. `JP#275`), else
from the file name; each file is copied into the bundle as
`configs/wireguard/<id>.conf` and mounted as `/gluetun/wireguard/wg0.conf`.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STUB = HERE / "stub"

TOKEN = re.compile(r"@@[A-Z_]+@@")

# --- modules -------------------------------------------------------------
# name -> template file for the service block. `--with-<name>` is generated
# from these keys, so adding a module is one template + one entry.
MODULES = {
    "searxng": {
        "template": "searxng-service.yaml",
        "ports": [8080],
        "readme": "| searxng | http `:8080` |",
    },
    "mitmproxy": {
        "template": "mitmproxy-service.yaml",
        "ports": [8181, 8081],
        "readme": "| mitmproxy | http `:8181`, web ui `:8081` (password in `.env`) |",
    },
    "debug": {
        "template": "debug-service.yaml",
        "ports": [9000],
        "requires": ["mitmproxy"],
        "readme": "| debug | http `:9000`, egress ip per exit |",
    },
}
# tolerated spellings
MODULE_ALIASES = {"searchxng": "searxng"}
# modules that don't add a service but do add files
SPECIAL_MODULES = {"stub": "test the LB without VPN keys (writes compose.stub.yaml)"}

# a Proton-style server name, e.g. JP#275, CH-JP#2, KM#25 - found in the conf
SERVER_NAME_RE = re.compile(r"\b([A-Z]{2}(?:-[A-Z]{2})?)#(\d{1,4})\b")


def fail(message: str) -> "None":
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def render(text: str, **tokens) -> str:
    for key, value in tokens.items():
        text = text.replace(f"@@{key}@@", str(value))
    leftover = sorted(set(TOKEN.findall(text)))
    if leftover:
        fail(f"unsubstituted placeholders: {', '.join(leftover)}")
    return text


def yq(value: str) -> str:
    """Quote a scalar for YAML (used for the debug targets JSON)."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def template(name: str) -> str:
    path = TEMPLATES / name
    if not path.is_file():
        fail(f"missing template: {path}")
    return path.read_text(encoding="utf-8")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(
        prog="generate.py",
        description="lbvpn - N WireGuard exits behind an nginx load balancer (+ consumer modules).",
    )
    parser.add_argument("--scan", metavar="DIR", required=True,
                        help="directory of WireGuard .conf files; one file = one exit")
    parser.add_argument("--lb-mode", default="rr", metavar="MODE",
                        help="pool listener policy: rr | <region> | <host:port> (default: rr)")
    parser.add_argument("--lb-port", type=int, default=8890, metavar="PORT",
                        help="pool listener port (default: 8890)")
    parser.add_argument("--region-port-base", type=int, default=3131, metavar="PORT",
                        help="first per-region listener port (default: 3131, +1 per region)")
    parser.add_argument("--bind", default="127.0.0.1", metavar="ADDR",
                        help="host address to publish on (default: 127.0.0.1, use 0.0.0.0 to expose)")
    parser.add_argument("--list-mods", action="store_true", help="list modules and exit")
    for name in sorted(MODULES):
        parser.add_argument(f"--with-{name}", action="store_true", help=f"bundle the {name} module")
    for alias in sorted(MODULE_ALIASES):
        parser.add_argument(f"--with-{alias}", action="store_true", help=argparse.SUPPRESS)
    for name, help_text in sorted(SPECIAL_MODULES.items()):
        parser.add_argument(f"--with-{name}", action="store_true", help=help_text)
    parser.add_argument("out", metavar="OUTPUT_DIR", nargs="?", help="folder to write into")
    args = parser.parse_args(argv)

    if args.list_mods:
        for name in sorted(MODULES):
            print(f"  --with-{name}")
        for name in sorted(SPECIAL_MODULES):
            print(f"  --with-{name}\t{SPECIAL_MODULES[name]}")
        return 0

    if not args.out:
        fail("OUTPUT_DIR is required")
    if args.bind not in ("127.0.0.1", "0.0.0.0") and not re.match(r"^\d+\.\d+\.\d+\.\d+$", args.bind):
        fail(f"--bind looks wrong: {args.bind}")

    scan_dir = Path(args.scan)
    if not scan_dir.is_dir():
        fail(f"--scan: directory not found: {scan_dir}")
    conf_paths = sorted(p for p in scan_dir.iterdir()
                        if p.is_file() and p.suffix.lower() == ".conf")
    if not conf_paths:
        fail(f"--scan: no .conf files in {scan_dir}")

    # --- scan: one file = one exit ------------------------------------------
    exits = []
    seen_ids = {}
    for path in conf_paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "[interface]" not in text.lower() or "privatekey" not in text.lower():
            fail(f"{path.name}: not a WireGuard config (no [Interface]/PrivateKey)")
        match = SERVER_NAME_RE.search(text)
        if match:
            label, label_source = f"{match.group(1)}#{match.group(2)}", "found in file"
        else:
            label, label_source = path.stem, "file name"
        ident = re.sub(r"[^a-z0-9]+", "", label.lower())
        if not ident:
            fail(f"{path.name}: could not derive a region id")
        if ident in seen_ids:
            fail(f"{path.name} and {seen_ids[ident]} both map to region id '{ident}' - rename one")
        seen_ids[ident] = path.name
        exits.append({"id": ident, "label": label, "source": label_source,
                      "src_path": path, "conf": f"{ident}.conf"})

    # --- region services ----------------------------------------------------
    region_blocks = []
    region_lines = []
    for index, exit_ in enumerate(exits):
        region = exit_["id"]
        port = args.region_port_base + index
        region_blocks.append(render(
            template("region-wireguard.yaml"), REGION=region, CONF=exit_["conf"],
        ).rstrip("\n"))
        region_lines.append(
            f"| `:{port}` | `vpn-{region}` | `{exit_['conf']}` | `{exit_['label']}` |"
        )

    # --- per-region listeners ----------------------------------------------
    region_endpoints = []
    port_lines = [f'      - "{args.bind}:{args.lb_port}:{args.lb_port}"']
    for index, exit_ in enumerate(exits):
        port = args.region_port_base + index
        region_endpoints.append(f"{port}=vpn-{exit_['id']}:8888")
        port_lines.append(f'      - "{args.bind}:{port}:{port}"')

    # --- modules ------------------------------------------------------------
    enabled = {n for n in MODULES if getattr(args, f"with_{n}")}
    for alias, target in MODULE_ALIASES.items():
        if getattr(args, f"with_{alias}"):
            enabled.add(target)
    special = {n for n in SPECIAL_MODULES if getattr(args, f"with_{n}")}
    for name in sorted(enabled):
        for dependency in MODULES[name].get("requires", []):
            if dependency not in enabled:
                fail(f"--with-{name} requires --with-{dependency}")

    mitm_password = secrets.token_hex(8)
    debug_targets = {"pool": "mitm:8080"}
    for index, exit_ in enumerate(exits):
        debug_targets[exit_["id"]] = f"lb:{args.region_port_base + index}"

    # --- published-port collision check -------------------------------------
    taken = {args.lb_port: "the lb pool"}
    for index, exit_ in enumerate(exits):
        taken[args.region_port_base + index] = f"exit {exit_['id']}"
    for name in sorted(enabled):
        for port in MODULES[name]["ports"]:
            if port in taken:
                fail(f"host port {port} is used by both {taken[port]} and the {name} module; "
                     f"change --lb-port/--region-port-base or disable one")
            taken[port] = f"the {name} module"

    module_blocks = []
    for name in sorted(enabled):
        block = template(MODULES[name]["template"])
        if name == "searxng":
            block = render(block, BIND=args.bind)
        elif name == "mitmproxy":
            block = render(block, LB_PORT=args.lb_port, BIND=args.bind,
                           MITM_PASSWORD=mitm_password)
        elif name == "debug":
            block = render(block, BIND=args.bind, TARGETS=yq(json.dumps(debug_targets)))
        module_blocks.append(block.rstrip("\n"))

    # --- assemble -----------------------------------------------------------
    project = re.sub(r"[^a-z0-9_-]+", "-", Path(args.out).resolve().name.lower()).strip("-_") or "lbvpn"
    invocation = "generate.py " + " ".join(argv)
    compose = render(
        template("compose.yaml"),
        PROJECT=project,
        INVOCATION=invocation,
        REGION_SERVICES="\n\n".join(region_blocks),
        LB_MODE=args.lb_mode,
        POOL_PORT=args.lb_port,
        VPN_HOSTS=" ".join(f"vpn-{e['id']}:8888" for e in exits),
        REGION_ENDPOINTS=" ".join(f"{args.region_port_base + i}=vpn-{e['id']}:8888"
                                  for i, e in enumerate(exits)),
        LB_PORTS="\n".join(port_lines),
        LB_DEPENDS="".join(f"\n      - vpn-{e['id']}" for e in exits),
        MODULES=("\n" + "\n\n".join(module_blocks)) if module_blocks else "",
    )

    module_env = ""
    if "mitmproxy" in enabled:
        module_env = ("\n# mitmproxy web ui password (http://localhost:8081)\n"
                      f"MITM_WEB_PASSWORD={mitm_password}\n")

    env_example = render(
        template("env.example"),
        LB_MODE=args.lb_mode,
        MODULE_ENV=module_env,
    )

    mod_readme = []
    if "searxng" in enabled:
        mod_readme.append("| `configs/searxng.yaml` | SearXNG settings (proxies -> the LB pool) |")
    for name in sorted(enabled - {"searxng"}):
        mod_readme.append(MODULES[name]["readme"])
    if "stub" in special:
        mod_readme.append("| `compose.stub.yaml` | run the LB without VPN keys |")

    readme = render(
        template("README.md"),
        PROJECT=project,
        REGION_COUNT=len(exits),
        REGION_LINES="\n".join(region_lines),
        LB_PORT=args.lb_port,
        FIRST_REGION_PORT=args.region_port_base,
        FIRST_REGION=exits[0]["id"],
        MODULE_FILES="\n".join(mod_readme),
    )

    # --- stub override -------------------------------------------------------
    stub_override = render(
        template("compose.stub.yaml"),
        REGION_OVERRIDES="\n".join(
            f"  vpn-{e['id']}:\n    image: lbvpn-stub\n    build: ./stub\n"
            f"    environment:\n      STUB_NAME: {e['id'].upper()}"
            for e in exits
        ),
    )

    # --- write --------------------------------------------------------------
    out = Path(args.out)
    configs = out / "configs"
    wg_dir = configs / "wireguard"
    configs.mkdir(parents=True, exist_ok=True)
    wg_dir.mkdir(parents=True, exist_ok=True)
    written = []

    def write(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)

    write(out / "compose.yaml", compose)
    write(out / ".env.example", env_example)
    write(out / "README.md", readme)
    write(configs / "lb-entrypoint.sh", template("lb-entrypoint.sh"))
    if "searxng" in enabled:
        write(configs / "searxng.yaml",
              render(template("searxng.yaml"), SECRET=secrets.token_hex(16), LB_PORT=args.lb_port))
    if "debug" in enabled:
        write(configs / "debug.py", template("debug.py"))
    for exit_ in exits:
        dst = wg_dir / exit_["conf"]
        shutil.copyfile(exit_["src_path"], dst)
        written.append(dst)
    if "stub" in special:
        if not (STUB / "proxy.py").is_file():
            fail(f"stub sources not found at {STUB}")
        shutil.copytree(STUB, out / "stub", dirs_exist_ok=True)
        write(out / "compose.stub.yaml", stub_override)
        written.extend(sorted(p for p in (out / "stub").iterdir() if p.is_file()))

    print(f"lbvpn: {len(exits)} exit(s) -> {out.resolve()}")
    for path in written:
        print(f"  {path.relative_to(out)}")
    print(f"\nnext:\n  cd {out} && docker compose up -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
