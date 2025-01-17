import itertools
import re
import sys
import time
from functools import partial
from typing import Dict, List, Optional, Sequence, Set, Tuple, TypedDict

import typer
import yaml
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from jhack.helpers import (
    ColorOption,
    JPopen,
    RichSupportedColorOptions,
    fetch_file,
    juju_status,
)
from jhack.logger import logger as jhack_logger

AppName = Endpoint = Interface = RemoteAppName = str
logger = jhack_logger.getChild("integrate")


class _AppEndpoints(TypedDict):
    requires: Dict[Endpoint, Tuple[Interface, List[RemoteAppName]]]
    provides: Dict[Endpoint, Tuple[Interface, List[RemoteAppName]]]


def _gather_endpoints(model=None, apps=()) -> Dict[AppName, _AppEndpoints]:
    status = juju_status(model=model, json=True)
    eps = {}

    def remotes(app, endpoint):
        if "relations" not in app:
            return []
        return app["relations"].get(endpoint, [])

    apps = status.get("applications")
    if not apps:
        sys.exit(f"No applications found in model {model}; does the model exist?")

    for app_name, app in apps.items():
        if apps and app_name not in apps:
            continue

        app_eps = {}
        unit = next(iter(app["units"]))
        metadata = fetch_file(unit, "metadata.yaml", model=model)
        meta = yaml.safe_load(metadata)

        for role in ("requires", "provides"):
            role_eps = {
                ep: (spec["interface"], remotes(app, ep))
                for ep, spec in meta.get(role, {}).items()
            }
            app_eps[role] = role_eps

        eps[app_name] = app_eps

    return eps


class IntegrationMatrix:
    def __init__(
        self,
        apps: str = None,
        model: str = None,
        color: RichSupportedColorOptions = "auto",
    ):
        self._model = model
        self._color = color
        self._endpoints = _gather_endpoints(model, apps)
        self._apps = tuple(sorted(self._endpoints))

        if apps:
            apps_re = re.compile(apps)
            self._apps = tuple(filter(lambda x: apps_re.match(x), self._apps))

        # X axis: requires
        # Y axis: provides
        self.matrix = self._build_matrix()

    def refresh(self):
        self._endpoints = _gather_endpoints(model=self._model, apps=self._apps)

    def _pairs(self):
        # returns provider, requirer pairs.
        return itertools.permutations(self._apps, 2)

    def _cells(self, skip_diagonal=True):
        for i, row in enumerate(self.matrix):
            for j, column in enumerate(row):
                if skip_diagonal and i == j:
                    continue
                yield column

    def _build_matrix(self):
        apps = self._apps
        mtrx = [[[] for _ in range(len(apps))] for _ in range(len(apps))]
        for provider, requirer in self._pairs():
            prov_idx = apps.index(provider)
            req_idx = apps.index(requirer)

            provides = self._endpoints[provider]["provides"]
            requires = self._endpoints[requirer]["requires"]
            shared = sorted(
                set(intf[0] for intf in provides.values()).intersection(
                    set(intf[0] for intf in requires.values())
                ),
                # sort by endpoint name
                key=lambda o: o[0],
            )

            mtrx[prov_idx][req_idx].extend(shared)
        return mtrx

    def _is_active(self, interface: str, provider: str, requirer: str):
        ep_to_apps = dict(self._endpoints[provider]["provides"].values())
        return requirer in ep_to_apps[interface]

    def _render_shared(self, app: str, shared: List[List[str]]):
        out = []
        for remote, lst in zip(self._apps, shared):
            t = Table(show_header=False, expand=True)
            t.add_column("")

            if not lst and remote != app:
                out.append(t)
                t.add_row(Text("- no interfaces - ", style="orange"))
                continue

            if remote == app:
                out.append(Text("-n/a-", style="orange"))
                continue

            for obj in lst:
                is_active = self._is_active(obj, app, remote)
                if is_active:
                    sym = "Y"
                    color = "green"
                else:
                    sym = "N"
                    color = "red"

                fmt_obj = obj + " " + sym
                t.add_row(Text(fmt_obj, style=color))
            out.append(t)

        return out

    def render(self, refresh: bool = False):
        if refresh:
            self.refresh()
        table = Table(title="integration  v0.1", expand=True)
        table.add_column(r"providers\requirers")

        for app in self._apps:
            table.add_column(app)

        for app, shared in zip(self._apps, self.matrix):
            table.add_row(app, *self._render_shared(app, shared))
        return Align.center(table)

    def pprint(self):
        c = Console(color_system=self._color)
        c.print(self.render())

    def watch(self, refresh_rate=0.2):
        rrate = refresh_rate or 0.2
        live = Live(
            get_renderable=partial(self.render, refresh=True), refresh_per_second=rrate
        )
        live.start()

        try:
            while True:
                time.sleep(rrate)
                live.refresh()

        except KeyboardInterrupt:
            print("aborting...")

        live.stop()
        live.console.clear_live()
        del live

    def _get_endpoint(self, app_name, role, interface):
        # get endpoint from interface name
        bindings = self._endpoints[app_name][role]
        for ep, (intf, _) in bindings.items():
            if intf == interface:
                return ep
        raise ValueError(f"cannot find binding for {interface} in {app_name}: {role}")

    def _get_interface(self, app_name, role, endpoint):
        # get interface name from endpoint
        try:
            return self._endpoints[app_name][role][endpoint][0]
        except KeyError as e:
            raise ValueError(
                f"cannot find interface for {endpoint} in {app_name}: {role}"
            ) from e

    def _apply_to_all(
        self,
        include: str,
        exclude: str,
        verb: str,
        juju_cmd: str,
        dry_run: bool = False,
        active: bool = None,
    ):
        targets = self._apps

        if include:
            inc_f = re.compile(include)
            targets = filter(lambda x: inc_f.match(x), targets)

        if exclude:
            exc_f = re.compile(exclude)
            targets = filter(lambda x: not exc_f.match(x), targets)

        target_apps = set(targets)
        logger.debug(f"target applications: {target_apps}")

        target_interfaces = []
        for interfaces, (prov, req) in zip(self._cells(), self._pairs()):

            for interface in interfaces:
                if active in {True, False}:
                    # only include if the interface is currently (in)active
                    if self._is_active(interface, prov, req) is not active:
                        logger.debug(
                            f"skipping {prov}:({interface}) <--> {req}: "
                            f'interface is {"in" if active else ""}active'
                        )
                        continue

                if prov not in target_apps:
                    logger.debug(f"skipping {prov}: not a target")
                    continue

                endpoint = self._get_endpoint(prov, "provides", interface)
                target_interfaces.append((f"{prov}:{endpoint}", req))

        logger.debug(f"target interfaces: {target_interfaces}")

        if not target_interfaces:
            print(f"Nothing to {verb}.")
            return

        if dry_run:
            print(f"would {verb}: {target_apps}")

        cmd_list: List[str] = []
        for ep1, ep2 in target_interfaces:
            cmd = f"juju {juju_cmd} {ep1} {ep2}"
            cmd_list.append(cmd)
            if dry_run:
                print(cmd)

        if dry_run:
            return

        print(f"{verb.title()}ing relations...")
        for cmd, (ep1, ep2) in zip(cmd_list, target_interfaces):
            print(f"\t{ep1} <--> {ep2}")
            JPopen(cmd.split(), wait=True)

        print("Done.")

    def connect(self, include: str = None, exclude: str = None, dry_run: bool = False):
        self._apply_to_all(
            include,
            exclude,
            verb="connect",
            juju_cmd="relate",
            dry_run=dry_run,
            active=False,
        )

    def disconnect(
        self, include: str = None, exclude: str = None, dry_run: bool = False
    ):
        self._apply_to_all(
            include,
            exclude,
            verb="disconnect",
            juju_cmd="remove-relation",
            dry_run=dry_run,
            active=True,
        )


# API
def link(
    include: str = typer.Option(
        None,
        "--include",
        "-i",
        help="Regex a provider will have to match to be included in the target pool",
    ),
    exclude: str = typer.Option(
        None,
        "--exclude",
        "-e",
        help="Regex a provider will have to NOT match to be included in the target pool",
    ),
    dry_run: bool = False,
    model: str = typer.Option(
        None, "--model", "-m", help="Model in which to apply this command."
    ),
):
    """Cross-relate applications in all possible ways."""
    IntegrationMatrix(model=model).connect(
        include=include, exclude=exclude, dry_run=dry_run
    )


def clear(
    include: str = typer.Option(
        None,
        "--include",
        "-i",
        help="Regex an application will have to match to be included in the target pool",
    ),
    exclude: str = typer.Option(
        None,
        "--exclude",
        "-e",
        help="Regex an application will have to NOT match to be included in the target pool",
    ),
    dry_run: bool = False,
    model: str = typer.Option(
        None, "--model", "-m", help="Model in which to apply this command."
    ),
):
    """Blanket-nuke relations between applications."""
    IntegrationMatrix(model=model).disconnect(
        include=include, exclude=exclude, dry_run=dry_run
    )


def show(
    apps: str = typer.Argument(
        None, help="Regex to filter the applications to include in the listing."
    ),
    watch: bool = typer.Option(
        None, "--watch", "-w", help="Keep this alive and refresh"
    ),
    refresh_rate: float = typer.Option(
        None, "--refresh-rate", "-r", help="Refresh rate for watch."
    ),
    model: str = typer.Option(
        None, "--model", "-m", help="Model in which to apply this command."
    ),
    color: Optional[str] = ColorOption,
):
    """Display the avaiable integrations between any number of juju applications in a nice matrix."""
    mtrx = IntegrationMatrix(apps=apps, model=model, color=color)
    if watch:
        mtrx.watch(refresh_rate=refresh_rate)
    else:
        mtrx.pprint()


def cmr(remote, local=None, dry_run: bool = False):
    """Command to pull a CMR over from some other model to the current one.

    Usage: jhack pull-cmr some-model

    A prompt will show, requesting you to pick which relation to create.
    Select one and you should be good to go! Enjoy.
    """
    return _cmr(remote, local=local, dry_run=dry_run)


def _cmr(remote, local=None, dry_run: bool = False):
    mtrx1 = IntegrationMatrix(model=local)
    mtrx2 = IntegrationMatrix(model=remote)
    apps1 = mtrx1._apps
    apps2 = mtrx2._apps

    cmrs = {}

    for provider, requirer in itertools.product(apps1, apps2):
        provides = mtrx1._endpoints[provider]["provides"]
        requires = mtrx2._endpoints[requirer]["requires"]
        shared = sorted(
            set(intf[0] for intf in provides.values()).intersection(
                set(intf[0] for intf in requires.values())
            ),
            # sort by endpoint name
            key=lambda o: o[0],
        )

        if shared:
            cmrs[(provider, requirer)] = shared

    opts = {}
    for i, ((prov, req), interfaces) in enumerate(cmrs.items()):
        for j, interface in enumerate(interfaces):
            print(f"({i}.{j}) := \t {prov} <-[{interface}]-> {req} ")
            opts[f"{i}.{j}"] = (prov, interface, req)

    cmr = Prompt.ask("Pick a CMR", choices=list(opts), default=list(opts)[0])

    prov, interface, req = opts[cmr]
    prov_endpoint = mtrx1._get_endpoint(prov, "provides", interface)
    req_endpoint = mtrx2._get_endpoint(req, "requires", interface)

    def fmt_endpoint(model, app, endpoint):
        return (
            Text(model or "<this model>", style="red")
            + "."
            + Text(app, style="purple")
            + ":"
            + Text(endpoint, style="cyan")
        )

    c = Console()
    txt = (
        Text("relating ")
        + fmt_endpoint(local, prov, prov_endpoint)
        + " <-["
        + Text(interface, style="green")
        + "]-> "
        + fmt_endpoint(remote, req, req_endpoint)
    )
    c.print(txt)

    script = [
        f"juju offer {remote}.{req}:{req_endpoint}",
        f"juju consume admin/{remote}.{req}",
        f"juju relate {req}:{req_endpoint} {prov}:{prov_endpoint}",
    ]

    if dry_run:
        print("would run:", "\n\t".join(script))
        return

    for cmd in script:
        if JPopen(cmd.split()).wait() != 0:
            print(f"{cmd} failed. Aborting...")
            return


if __name__ == "__main__":
    mtrx = IntegrationMatrix()
    # mtrx.watch()
    # mtrx.pprint()
    mtrx.connect(dry_run=True, include="prom")
    mtrx.disconnect(dry_run=True)
