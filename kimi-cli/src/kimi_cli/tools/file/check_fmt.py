import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

import orjson
import yaml


def check_json_text(text: str, json_callback: Callable[[Any], None] | None = None) -> str | None:
    """Validate the format of a JSON string.

    Args:
        text: JSON text to validate.
        json_callback: Optional callback invoked with the parsed object.

    Returns:
        None if the JSON is valid, error message string otherwise.
    """
    try:
        js = orjson.loads(text)
        if json_callback is not None:
            json_callback(js)
        return None
    except orjson.JSONDecodeError as exc:
        return f"JSON decode error at line {exc.lineno}, column {exc.colno}: {exc.msg}"
    except Exception as exc:
        return f"failed to validate JSON file: {str(exc)}"

def check_xml_text(text: str, xml_callback: Callable[[Any], None] | None = None) -> str | None:
    """Validate the format of an XML string.

    Args:
        text: XML text to validate.
        xml_callback: Optional callback invoked with the parsed tree.

    Returns:
        None if the XML is valid, error message string otherwise.
    """
    try:
        tree = ET.fromstring(text)
        if xml_callback is not None:
            xml_callback(tree)
        return None
    except ET.ParseError as exc:
        return f"XML parse error: {str(exc)}"
    except Exception as exc:
        return f"failed to validate XML file: {str(exc)}"

def check_yaml_text(text: str, yaml_callback: Callable[[Any], None] | None = None) -> str | None:
    """Validate the format of a YAML string.

    Args:
        text: YAML text to validate.
        yaml_callback: Optional callback invoked with the parsed object.

    Returns:
        None if the YAML is valid, error message string otherwise.
    """
    try:
        data = yaml.safe_load(text)
        if yaml_callback is not None:
            yaml_callback(data)
        return None
    except yaml.YAMLError as exc:
        return f"YAML parse error: {str(exc)}"
    except Exception as exc:
        return f"failed to validate YAML file: {str(exc)}"

def check_toml_text(text: str, toml_callback: Callable[[Any], None] | None = None) -> str | None:
    """Validate the format of a TOML string.

    Args:
        text: TOML text to validate.
        toml_callback: Optional callback invoked with the parsed object.

    Returns:
        None if the TOML is valid, error message string otherwise.
    """
    try:
        data = tomllib.loads(text)
        if toml_callback is not None:
            toml_callback(data)
        return None
    except tomllib.TOMLDecodeError as exc:
        return f"TOML parse error: {str(exc)}"
    except Exception as exc:
        return f"failed to validate TOML file: {str(exc)}"

