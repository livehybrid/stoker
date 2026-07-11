"""Static and smoke tests for the vendored splunk_eventgen 7.2.1 tree.

Static checks run on any python >= 3.9 with no third-party deps. The smoke
tests exercise the generate path end to end and skip when the engine's
runtime deps (worker/requirements.txt) are not installed.
"""
from __future__ import annotations

import ast
import os
import py_compile
import subprocess
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.join(os.path.dirname(TESTS_DIR), "engines", "eventgen")
PACKAGE_DIR = os.path.join(ENGINE_DIR, "splunk_eventgen")

# Modules whose import anywhere in the vendored tree means a deletion leaked.
FORBIDDEN_ROOTS = {
    "six",
    "ujson",
    "httplib2",
    "flask",
    "redis",
    "boto3",
    "botocore",
    "requests_futures",
    "imp",
}
FORBIDDEN_INTERNAL = {
    "splunk_eventgen.eventgen_api_server",
    "splunk_eventgen.splunk_app",
}

# Stdlib roots the vendored tree is allowed to use; anything imported at
# module level outside this list must be jinja2 or dateutil.
STDLIB_ROOTS = {
    "argparse", "ast", "collections", "configparser", "cProfile", "csv",
    "datetime", "errno", "gzip", "importlib", "io", "json", "logging",
    "math", "multiprocessing", "os", "pprint", "queue", "random", "re",
    "shutil", "signal", "socket", "ssl", "string", "struct", "sys",
    "tarfile", "threading", "time", "traceback", "types", "urllib", "uuid",
    "xml", "__future__",
}
ALLOWED_THIRD_PARTY = {"jinja2", "dateutil"}
# Lazy imports on Splunk-embedded paths that never execute standalone.
ALLOWED_LAZY = {"splunk"}


def _python_files():
    found = []
    for dirpath, dirnames, filenames in os.walk(PACKAGE_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if filename.endswith(".py"):
                found.append(os.path.join(dirpath, filename))
    assert found, "vendored tree missing at {}".format(PACKAGE_DIR)
    return sorted(found)


def _imports(path):
    """Yield (root_module, is_module_level) for every import in the file."""
    with open(path) as handle:
        tree = ast.parse(handle.read(), path)
    module_level_nodes = set(ast.iter_child_nodes(tree))
    for node in ast.walk(tree):
        top = node in module_level_nodes
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, top
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, top


def test_deleted_paths_absent():
    assert not os.path.exists(os.path.join(PACKAGE_DIR, "eventgen_api_server"))
    assert not os.path.exists(os.path.join(PACKAGE_DIR, "splunk_app"))
    assert not os.path.exists(os.path.join(PACKAGE_DIR, "identitygen.py"))
    output_dir = os.path.join(PACKAGE_DIR, "lib", "plugins", "output")
    for deleted in (
        "httpevent.py", "httpevent_core.py", "metric_httpevent.py",
        "awss3.py", "scsout.py", "splunkstream.py", "tcpout.py",
        "udpout.py", "s2s.py", "syslogout.py",
    ):
        assert not os.path.exists(os.path.join(output_dir, deleted)), deleted
    for kept in ("stdout.py", "devnull.py", "file.py", "modinput.py"):
        assert os.path.exists(os.path.join(output_dir, kept)), kept


def test_every_file_compiles(tmp_path):
    for index, path in enumerate(_python_files()):
        cfile = os.path.join(str(tmp_path), "{}.pyc".format(index))
        py_compile.compile(path, cfile=cfile, doraise=True)


def test_no_forbidden_imports():
    offences = []
    for path in _python_files():
        for module, _ in _imports(path):
            root = module.split(".")[0]
            if root in FORBIDDEN_ROOTS:
                offences.append((path, module))
            if any(module.startswith(bad) for bad in FORBIDDEN_INTERNAL):
                offences.append((path, module))
    assert offences == []


def test_third_party_surface_is_minimal():
    module_level = set()
    lazy = set()
    for path in _python_files():
        for module, top in _imports(path):
            root = module.split(".")[0]
            if root in STDLIB_ROOTS or root == "splunk_eventgen":
                continue
            if top:
                module_level.add(root)
            else:
                lazy.add(root)
    assert module_level <= ALLOWED_THIRD_PARTY, module_level
    assert lazy <= ALLOWED_THIRD_PARTY | STDLIB_ROOTS | ALLOWED_LAZY, lazy


def _run_generate(conf_path, tmp_path, extra_args=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = ENGINE_DIR
    log_dir = os.path.join(str(tmp_path), "eglogs")
    os.makedirs(log_dir, exist_ok=True)
    env["EVENTGEN_LOG_DIR"] = log_dir
    cmd = [sys.executable, "-m", "splunk_eventgen", "generate"]
    cmd.extend(extra_args or [])
    cmd.append(conf_path)
    return subprocess.run(
        cmd, env=env, cwd=str(tmp_path), timeout=120,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _write_pack(tmp_path, conf_text, samples=None, templates=None):
    pack = os.path.join(str(tmp_path), "pack")
    os.makedirs(os.path.join(pack, "default"))
    os.makedirs(os.path.join(pack, "samples", "templates"))
    conf_path = os.path.join(pack, "default", "eventgen.conf")
    with open(conf_path, "w") as handle:
        handle.write(conf_text)
    for name, body in (samples or {}).items():
        with open(os.path.join(pack, "samples", name), "w") as handle:
            handle.write(body)
    for name, body in (templates or {}).items():
        path = os.path.join(pack, "samples", "templates", name)
        with open(path, "w") as handle:
            handle.write(body)
    return conf_path


def test_windbag_generate_smoke(tmp_path):
    pytest.importorskip("dateutil")
    conf = _write_pack(
        tmp_path,
        "[windbag]\n"
        "generator = windbag\n"
        "count = 5\n"
        "interval = 1\n"
        "end = 1\n"
        "outputMode = stdout\n",
    )
    result = _run_generate(conf, tmp_path)
    assert result.returncode == 0, result.stderr.decode()
    lines = [l for l in result.stdout.decode().splitlines() if "WINDBAG" in l]
    assert len(lines) == 5, result.stdout.decode()


def test_sample_file_generate_with_timestamp_token(tmp_path):
    pytest.importorskip("dateutil")
    conf = _write_pack(
        tmp_path,
        "[web.sample]\n"
        "generator = default\n"
        "count = 4\n"
        "interval = 1\n"
        "end = 1\n"
        "outputMode = stdout\n"
        "token.0.token = \\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}\n"
        "token.0.replacementType = timestamp\n"
        "token.0.replacement = %Y-%m-%d %H:%M:%S\n",
        samples={
            "web.sample": (
                "2020-01-01 00:00:00 GET /a 200\n"
                "2020-01-01 00:00:01 GET /b 404\n"
            )
        },
    )
    result = _run_generate(conf, tmp_path)
    assert result.returncode == 0, result.stderr.decode()
    lines = result.stdout.decode().splitlines()
    assert len(lines) == 4, result.stdout.decode()
    # every timestamp must have been replaced away from the 2020 fixture value
    assert all("2020-01-01" not in line for line in lines), lines


def test_jinja_generate_smoke(tmp_path):
    pytest.importorskip("dateutil")
    pytest.importorskip("jinja2")
    conf = _write_pack(
        tmp_path,
        "[jinjastanza]\n"
        "generator = jinja\n"
        "jinja_template_dir = templates\n"
        "jinja_target_template = test.template\n"
        'jinja_variables = {"loops": 3}\n'
        "count = 1\n"
        "interval = 1\n"
        "end = 1\n"
        "outputMode = stdout\n",
        templates={
            "test.template": (
                "{% for n in range(loops) %}\n"
                '{"_time": {{ eventgen_target_time_epoch }},'
                ' "_raw": "jinja event {{ n }}"}\n'
                "{% endfor %}\n"
            )
        },
    )
    result = _run_generate(conf, tmp_path)
    assert result.returncode == 0, result.stderr.decode()
    lines = [
        l for l in result.stdout.decode().splitlines() if "jinja event" in l
    ]
    assert len(lines) == 3, result.stdout.decode()
