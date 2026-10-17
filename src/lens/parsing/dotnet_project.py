"""Safe structural extraction for .NET project and solution manifests."""
import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from lens.core.models import CodeSymbol, Knowledge, Relationship, stable_id

PROPERTIES = {"targetframework", "targetframeworks", "targetframeworkversion", "assemblyname",
              "outputtype", "rootnamespace", "nullable", "implicitusings", "langversion"}


def parse(file, data):
    source = data.decode("utf-8-sig")
    result = Knowledge()

    def add(name, kind, line, excerpt, metadata=None):
        qualified = f"{file.path}::{name}"
        symbol = CodeSymbol(stable_id(file.id, kind, qualified, str(line)), name, kind,
                            qualified, file.id, line, line, excerpt, metadata or {})
        result.symbols.append(symbol)
        return symbol

    if PurePosixPath(file.path).suffix.lower() == ".sln":
        for line_no, line in enumerate(source.splitlines(), 1):
            match = re.match(r'^Project\("[^\"]+"\)\s*=\s*"([^\"]+)"\s*,\s*"([^\"]+)"', line)
            if not match:
                continue
            name, relative = match.groups()
            project = add(name, "dotnet_project", line_no, f"Project {name}: {relative}",
                          {"project_path": relative.replace("\\", "/")})
            result.relationships.append(Relationship(project.id, "REFERENCES_PROJECT", relative,
                                                      evidence="solution project entry"))
        return result

    try:
        document = ET.fromstring(source)
    except ET.ParseError:
        result.warnings.append(f"{file.path}: invalid .NET project XML; project metadata omitted")
        return result

    lines = source.splitlines()

    def line_for(token):
        for index, line in enumerate(lines, 1):
            if token in line:
                return index
        return 1

    project = add(PurePosixPath(file.path).name, "dotnet_project", 1,
                  f".NET project manifest {PurePosixPath(file.path).name}",
                  {"manifest_type": PurePosixPath(file.path).suffix.lower()})
    for node in document.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        folded = tag.lower()
        if folded in PROPERTIES and (node.text or "").strip():
            value = (node.text or "").strip()
            line = line_for(f"<{tag}")
            item = add(tag, "dotnet_setting", line, f"{tag}={value}", {"value": value})
            result.relationships.append(Relationship(project.id, "CONFIGURES", tag,
                                                      item.id, "project property", "high"))
        elif folded in {"packagereference", "frameworkreference", "projectreference"}:
            include = next((value for key, value in node.attrib.items()
                            if key.lower() in {"include", "update"}), "")
            if not include:
                continue
            version = next((value for key, value in node.attrib.items() if key.lower() == "version"), "")
            if not version:
                version = next(((child.text or "").strip() for child in node
                                if child.tag.rsplit("}", 1)[-1].lower() == "version"), "")
            kind = "dotnet_project_reference" if folded == "projectreference" else "dotnet_package"
            line = line_for(include)
            excerpt = f"{tag} {include}" + (f" version {version}" if version else "")
            item = add(include, kind, line, excerpt,
                       {"include": include, "version": version, "reference_type": folded})
            result.relationships.append(Relationship(project.id, "REFERENCES_PROJECT" if kind.endswith("reference")
                                                      else "USES_PACKAGE", include, item.id,
                                                      "project manifest reference", "high"))
    return result
