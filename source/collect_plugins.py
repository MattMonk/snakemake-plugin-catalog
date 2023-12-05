from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, List
import uuid

import requests
from ratelimit import limits, sleep_and_retry
from jinja2 import Environment, FileSystemLoader, select_autoescape
import m2r2


@sleep_and_retry
@limits(calls=20, period=1)
def pypi_api(query, accept="application/json"):
    return requests.get(
        query,
        headers={
            "Accept": accept,
            "User-Agent": "Snakemake plugin catalog (https://github.com/snakemake/snakemake-plugin-catalog)",
        },
    ).json()


class MetadataCollector:
    def __init__(self, package: str, plugin_type: str):
        self.envname = uuid.uuid4().hex
        self.package = package
        self.plugin_type = plugin_type

    def __enter__(self):
        subprocess.run(
            f"micromamba create -n {self.envname} -y python pip",
            check=True,
            shell=True,
        )
        subprocess.run(
            f"micromamba run -n {self.envname} pip install git+https://github.com/snakemake/snakemake.git {self.package}",
            shell=True,
            check=True,
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        subprocess.run(
            f"micromamba env remove -n {self.envname} -y", check=True, shell=True
        )

    def extract_info(self, statement: str) -> str:
        registry = f"{self.plugin_type.title()}PluginRegistry"
        plugin_name = self.package.removeprefix(f"snakemake-{self.plugin_type}-plugin-")
        res = subprocess.run(
            f"micromamba run -n {self.envname} python -c \"from snakemake_interface_{self.plugin_type}_plugins.registry import {registry}; plugin = {registry}().get_plugin('{plugin_name}'); {statement}\"",
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
        )
        return res.stdout.decode()

    def get_settings(self) -> List[Dict[str, Any]]:
        info = self.extract_info(
            "import json; "
            "fmt_type = lambda thetype: thetype.__name__ if thetype is not None else None; "
            "fmt_setting_item = lambda key, value: (key, fmt_type(value)) if key == 'type' else (key, value); "
            "fmt_setting = lambda setting: dict(map(lambda item: fmt_setting_item(*item), setting.items())); "
            "print(json.dumps(list(map(fmt_setting, plugin.get_settings_info()))))"
        )
        return json.loads(info)


class PluginCollectorBase(ABC):
    @abstractmethod
    def plugin_type(self) -> str:
        raise NotImplementedError()

    def aux_info(self, metadata_collector) -> Dict[str, Any]:
        return {}

    def collect_plugins(self, plugins, data, templates):
        plugin_type = self.plugin_type()
        plugin_dir = Path("plugins") / plugin_type
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir)
        plugin_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"snakemake-{plugin_type}-plugin-"
        packages = [
            project["name"]
            for project in data["projects"]
            if project["name"].startswith(prefix)
        ]
        for package in packages:
            # if package != "snakemake-storage-plugin-s3" and package != "snakemake-storage-plugin-fs":
            #     continue
            meta = pypi_api(f"https://pypi.org/pypi/{package}/json")
            plugin_name = package.removeprefix(prefix)
            desc = "\n".join(meta["info"]["description"].split("\n")[2:])

            # convert to rst
            desc = m2r2.convert(desc)

            with MetadataCollector(package, plugin_type) as collector:
                settings = collector.get_settings()
                aux_info = self.aux_info(collector)

            def get_setting_meta(setting, key, default="", verb=False):
                value = setting.get(key, default)
                if verb:
                    return f"``{repr(value)}``"
                elif isinstance(value, list):
                    return ", ".join(value)
                elif isinstance(value, bool):
                    return "✓" if value else "✗"
                elif value is None:
                    return default
                return value

            rendered = templates.get_template(f"{plugin_type}_plugin.rst.j2").render(
                plugin_name=plugin_name,
                package_name=package,
                meta=meta,
                desc=desc,
                plugin_type=plugin_type,
                settings=settings,
                get_setting_meta=get_setting_meta,
                **aux_info,
            )
            with open((plugin_dir / plugin_name).with_suffix(".rst"), "w") as f:
                f.write(rendered)
            plugins[plugin_type].append(plugin_name)


class ExecutorPluginCollector(PluginCollectorBase):
    def plugin_type(self) -> str:
        return "executor"


class StoragePluginCollector(PluginCollectorBase):
    def plugin_type(self) -> str:
        return "storage"

    def aux_info(self, metadata_collector) -> Dict[str, Any]:
        info = metadata_collector.extract_info(
            "import json; "
            "queries = plugin.storage_provider.example_queries(); "
            "print(json.dumps({'example_queries': ["
            "{'query': qry.query, 'desc': qry.description, 'type': qry.type.name.lower()} "
            "for qry in queries]}))"
        )
        return json.loads(info)


def collect_plugins():
    templates = Environment(
        loader=FileSystemLoader("_templates"),
        autoescape=select_autoescape(),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    plugins = defaultdict(list)
    data = pypi_api(
        "https://pypi.org/simple/", accept="application/vnd.pypi.simple.v1+json"
    )

    for collector in (ExecutorPluginCollector, StoragePluginCollector):
        collector().collect_plugins(plugins, data, templates)

    with open("index.rst", "w") as f:
        f.write(templates.get_template("index.rst.j2").render(plugins=plugins))
