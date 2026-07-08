"""JSON file validator tool."""
import xml.etree.ElementTree as ET
import orjson


def check_json(file_path: str, json_callback = None) -> str | None:
    """Validate the format of a JSON file.

    Args:
        file_path: Path to the JSON file to validate.

    Returns:
        None if the JSON file is valid, error message string otherwise.
    """
    try:
        js = None
        with open(file_path, 'r', encoding='utf-8') as f:
            js = orjson.loads(f.read())
        if json_callback:
            json_callback(js)
        return None

    except orjson.JSONDecodeError as exc:
        return f"JSON decode error at line {exc.lineno}, column {exc.colno}: {exc.msg}"
    except Exception as exc:
        return f"Failed to validate JSON file: {str(exc)}"


"""XML file validator tool."""


def check_xml(file_path: str, xml_callback = None) -> str | None:
    """Validate the format of an XML file.

    Args:
        file_path: Path to the XML file to validate.

    Returns:
        None if the XML file is valid, error message string otherwise.
    """
    try:
        tree = ET.parse(file_path)
        if xml_callback:
            xml_callback(tree)
        return None

    except ET.ParseError as exc:
        return f"XML parse error: {str(exc)}"
    except Exception as exc:
        return f"Failed to validate XML file: {str(exc)}"


def check_json_str(content: str, json_callback=None) -> str | None:
    """Validate the format of a JSON string.

    Args:
        content: JSON string content to validate.
        json_callback: Optional callback function to process the parsed JSON object.

    Returns:
        None if the JSON string is valid, error message string otherwise.
    """
    try:
        js = orjson.loads(content)
        if json_callback:
            json_callback(js)
        return None

    except orjson.JSONDecodeError as exc:
        return f"JSON decode error at line {exc.lineno}, column {exc.colno}: {exc.msg}"
    except Exception as exc:
        return f"Failed to validate JSON content: {str(exc)}"


def check_xml_str(content: str, xml_callback=None) -> str | None:
    """Validate the format of an XML string.

    Args:
        content: XML string content to validate.
        xml_callback: Optional callback function to process the parsed XML ElementTree.

    Returns:
        None if the XML string is valid, error message string otherwise.
    """
    try:
        root = ET.fromstring(content)
        if xml_callback:
            xml_callback(root)
        return None

    except ET.ParseError as exc:
        return f"XML parse error: {str(exc)}"
    except Exception as exc:
        return f"Failed to validate XML content: {str(exc)}"
