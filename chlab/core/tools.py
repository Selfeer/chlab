"""The tools container: where scripts that create or inspect data run.

It joins the instance's network with every service already configured in its environment
(see each service's ``tool_env``), the invoking directory mounted at ``/work``, and the
``chlab_tools`` helper module importable. Nothing is installed on the host.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from . import services as companions
from .services.catalogs import RestCatalog
from .spec import ClusterSpec
from .topology import CONTAINER_HTTP_PORT, build_topology

WORKDIR = "/work"
LIBDIR = "/chlab/lib"
#: The helper module shipped with chlab, mounted rather than baked into the image so it
#: always matches the chlab that runs it.
TOOLBOX = Path(__file__).resolve().parent.parent / "toolbox"


def environment(spec: ClusterSpec) -> dict[str, str]:
    topology = build_topology(spec)
    catalogs = [
        service.pyiceberg_name
        for service, _ in companions.active(topology)
        if isinstance(service, RestCatalog)
    ]
    env = {
        "CHLAB_INSTANCE": spec.name,
        "CHLAB_CLUSTER": spec.cluster_name,
        "CHLAB_NODES": json.dumps(
            {node.name: f"http://{node.name}:{CONTAINER_HTTP_PORT}" for node in topology.nodes}
        ),
        "CHLAB_CATALOGS": ",".join(catalogs),
        "PYTHONPATH": LIBDIR,
    }
    if catalogs:
        env["PYICEBERG_DEFAULT_CATALOG"] = catalogs[0]
    for service, endpoint in companions.active(topology):
        env.update(service.tool_env(topology, endpoint))
    return env


def tools_command(
    spec: ClusterSpec,
    command: Iterable[str],
    *,
    image: str,
    workdir: Path,
    mounts: Iterable[tuple[str, str]] = (),
    interactive: bool = False,
    tty: bool = False,
) -> list[str]:
    """``docker run`` of ``command`` in the tools image, on the instance's network."""
    args = ["docker", "run", "--rm"]
    if interactive:
        args.append("-i")
    if tty:
        args.append("-t")
    args += ["--network", spec.network_name]
    if hasattr(os, "getuid"):
        # Files a script writes into /work belong to the user who ran it, not root.
        args += ["--user", f"{os.getuid()}:{os.getgid()}"]
    for key, value in environment(spec).items():
        args += ["-e", f"{key}={value}"]
    args += ["-v", f"{workdir.resolve()}:{WORKDIR}", "-w", WORKDIR]
    args += ["-v", f"{TOOLBOX}:{LIBDIR}:ro"]
    for host_path, container_path in mounts:
        args += ["-v", f"{host_path}:{container_path}:ro"]
    return [*args, image, *command]
