"""Environment preflight for the charge-sheet v3 package.

    python preflight.py            # everything except a live model call
    python preflight.py --with-ai  # also make one tiny live model call

Exit code 0 means the package can run.
"""
from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "
problems: list[str] = []


def line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))


def check_python() -> None:
    v = sys.version_info
    if v >= (3, 10):
        line(OK, "Python", platform.python_version())
    else:
        line(BAD, "Python", f"{platform.python_version()} — need 3.10+")
        problems.append("python")


def check_imports() -> None:
    for mod, pip in (("cv2", "opencv-python-headless"), ("numpy", "numpy"),
                     ("PIL", "Pillow"), ("anthropic", "anthropic")):
        try:
            m = __import__(mod)
            line(OK, mod, getattr(m, "__version__", ""))
        except ImportError:
            line(BAD, mod, f"missing — pip install {pip}")
            problems.append(mod)
    for mod, pip in (("azure.identity", "azure-identity"),
                     ("azure.keyvault.secrets", "azure-keyvault-secrets")):
        try:
            __import__(mod)
            line(OK, mod)
        except ImportError:
            line(WARN, mod, f"missing — only needed for the Key Vault auth path "
                            f"(pip install {pip})")


def check_poppler() -> None:
    try:
        from render import pdftoppm_path
        exe = pdftoppm_path()
        out = subprocess.run([exe, "-v"], capture_output=True, text=True)
        ver = (out.stderr or out.stdout).strip().splitlines()[0] if (out.stderr or out.stdout) else ""
        line(OK, "pdftoppm", f"{exe}  {ver}")
    except Exception as exc:
        line(BAD, "pdftoppm", str(exc).splitlines()[0])
        problems.append("poppler")


def check_template() -> None:
    try:
        from template_registry import load_manifest_and_catalog, locked_template
        manifest, catalog, ref = load_manifest_and_catalog()
        t = locked_template()
        line(OK, "locked template",
             f"{manifest['template_id']} v{manifest['version']}, "
             f"{len(catalog['cells'])} rows, reference {t.w}x{t.h}, "
             f"renderer dpi {manifest['renderer']['dpi']}")
    except Exception as exc:
        line(BAD, "locked template", str(exc).splitlines()[0])
        problems.append("template")


def check_modules() -> None:
    for m in ("settings", "alignment", "handwriting", "mark_localizer",
              "mark_adjudicator", "page_audit", "render", "dates",
              "header_extract", "model_client", "template_registry"):
        try:
            __import__(m)
            line(OK, f"module {m}")
        except Exception as exc:
            line(BAD, f"module {m}", str(exc).splitlines()[0])
            problems.append(m)


def check_auth(live: bool) -> None:
    import os
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        line(OK, "credentials", "explicit ANTHROPIC_API_KEY override is set")
    else:
        try:
            from settings import get_settings
            s = get_settings()
            line(OK, "credentials",
                 "shared EOB_v9 Key Vault path: "
                 f"{s.key_vault_url} / secret {s.anthropic_key_secret} "
                 "(`az login` required locally)")
        except Exception as exc:
            line(WARN, "credentials", f"shared Key Vault config unreadable: {exc}")
    if not live:
        return
    try:
        from model_client import make_client
        from settings import get_settings
        c = make_client()
        s = get_settings()
        r = c.messages.create(model=s.mark_model, max_tokens=8,
                              messages=[{"role": "user", "content": "Reply with: ok"}])
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        line(OK, "live model call", f"{s.mark_model} -> {text.strip()[:20]}")
    except Exception as exc:
        line(BAD, "live model call", str(exc).splitlines()[0])
        problems.append("model")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-ai", action="store_true",
                    help="also make one live model call")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    print(f"charge-sheet v3 preflight — {platform.system()} {platform.release()}\n")
    check_python()
    check_imports()
    check_poppler()
    check_modules()
    check_template()
    check_auth(args.with_ai)

    print()
    if problems:
        print(f"FAILED: {', '.join(sorted(set(problems)))}")
        return 1
    print("All required checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
