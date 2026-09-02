#!/usr/bin/env python3
"""
Logging configuration for the Text-to-GraphQL MCP Server using loguru.
"""

import os
import sys
from datetime import datetime
from loguru import logger as loguru_logger
import json
from urllib.parse import urlsplit, urlunsplit

# Remove default logger
loguru_logger.remove()

# Setup logs directory - handle read-only file systems gracefully
try:
    os.makedirs("logs", exist_ok=True)
    LOGS_AVAILABLE = True
except (OSError, PermissionError):
    # Running in read-only environment (like MCP), skip file logging
    LOGS_AVAILABLE = False

# Detect if running in MCP mode
# MCP servers use stdin/stdout for JSON-RPC communication
def is_mcp_mode():
    """Detect if we're running as an MCP server."""
    # Check if stdin is not a tty (typical for MCP servers)
    # or if we're explicitly told we're in MCP mode
    return (
        not sys.stdin.isatty() or 
        os.environ.get('MCP_MODE', '').lower() == 'true' or
        os.environ.get('RUNNING_AS_MCP_SERVER', '').lower() == 'true'
    )

MCP_MODE = is_mcp_mode()

def pretty_json(value):
    """Format JSON nicely for logs with proper indentation and colors."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, default=str, ensure_ascii=False)
    return str(value)

def setup_logger(name="app", log_level="INFO"):
    """
    Configure and return a logger with the given name and log level
    
    Args:
        name: Name of the logger
        log_level: Logging level to use
        
    Returns:
        A configured logger instance
    """
    # Create current date for log file naming
    current_date = datetime.now().strftime("%Y-%m-%d")
    log_file = f"logs/{name}_{current_date}.log"
    
    # Choose output stream and formatting based on MCP mode
    if MCP_MODE:
        # In MCP mode: send logs to stderr, disable colors
        output_stream = sys.stderr
        use_colors = False
        log_format = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name} | {function}:{line} | {message}"
    else:
        # Normal mode: send logs to stdout with colors
        output_stream = sys.stdout
        use_colors = True
        log_format = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> | <blue>{function}:{line}</blue> | <level>{message}</level>"
    
    # Console handler with appropriate formatting
    loguru_logger.add(
        output_stream,
        level=log_level,
        format=log_format,
        filter=lambda record: record["extra"].get("name", "") == name or not record["extra"].get("name"),
        colorize=use_colors,
        enqueue=True,  # Thread-safe logging
    )
    
    # File handler with rotation (only if logs directory is available)
    if LOGS_AVAILABLE:
        loguru_logger.add(
            log_file,
            level=log_level,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name} | {function}:{line} | {message}",
            filter=lambda record: record["extra"].get("name", "") == name or not record["extra"].get("name"),
            rotation="10 MB",
            compression="zip",
            retention="1 week",
            enqueue=True,  # Thread-safe logging
        )
    
    # Create a contextualized logger
    contextualized_logger = loguru_logger.bind(name=name)
    
    # Add a special method for JSON logging
    def log_json(level, message, data, **kwargs):
        # Central choke point: library error strings routinely embed the request
        # URL, so scrub known secrets out of every structured record rather than
        # trying to anticipate each message shape.
        pretty = scrub_secrets(pretty_json(data))
        if MCP_MODE:
            # No colors in MCP mode
            contextualized_logger.log(level, f"{message}\n{pretty}", **kwargs)
        else:
            # Colors in normal mode
            contextualized_logger.opt(colors=True).log(
                level, 
                f"{message}\n<magenta>{pretty}</magenta>", 
                **kwargs
            )
    
    # Add a method for logging LLM prompts
    def log_llm(message, prompt=None, response=None, **kwargs):
        log_message = f"{message}"
        
        if prompt:
            prompt_str = prompt if isinstance(prompt, str) else pretty_json(prompt)
            if MCP_MODE:
                log_message += f"\nPrompt:\n{prompt_str}"
            else:
                log_message += f"\n<yellow>Prompt:</yellow>\n<yellow>{prompt_str}</yellow>"
            
        if response:
            response_str = response if isinstance(response, str) else pretty_json(response)
            if MCP_MODE:
                log_message += f"\nResponse:\n{response_str}"
            else:
                log_message += f"\n<green>Response:</green>\n<green>{response_str}</green>"
        
        if MCP_MODE:
            contextualized_logger.info(log_message, **kwargs)
        else:
            contextualized_logger.opt(colors=True).info(log_message, **kwargs)
    
    # Add a method for logging GraphQL queries
    def log_graphql(message, query=None, variables=None, result=None, error=None, **kwargs):
        log_message = f"{message}"
        
        if query:
            query_str = query.strip() if isinstance(query, str) else pretty_json(query)
            if MCP_MODE:
                log_message += f"\nQuery:\n{query_str}"
            else:
                log_message += f"\n<cyan>Query:</cyan>\n<cyan>{query_str}</cyan>"
        
        if variables:
            variables_str = pretty_json(variables)
            if MCP_MODE:
                log_message += f"\nVariables:\n{variables_str}"
            else:
                log_message += f"\n<blue>Variables:</blue>\n<blue>{variables_str}</blue>"
        
        if result:
            result_str = pretty_json(result)
            if MCP_MODE:
                log_message += f"\nResult:\n{result_str}"
            else:
                log_message += f"\n<green>Result:</green>\n<green>{result_str}</green>"
            
        if error:
            error_str = error if isinstance(error, str) else pretty_json(error)
            if MCP_MODE:
                log_message += f"\nError:\n{error_str}"
            else:
                log_message += f"\n<red>Error:</red>\n<red>{error_str}</red>"
            
        level = "ERROR" if error else "INFO"
        if MCP_MODE:
            contextualized_logger.log(level, log_message, **kwargs)
        else:
            contextualized_logger.opt(colors=True).log(level, log_message, **kwargs)
    
    # Add methods to the logger
    contextualized_logger.json = log_json
    contextualized_logger.llm = log_llm
    contextualized_logger.graphql = log_graphql
    
    return contextualized_logger

# Create a default application logger
logger = setup_logger()


def redact_url(url) -> str:
    """
    Strip credential material from a URL so it is safe to log or return.

    Removes any userinfo (https://user:pass@host) plus the query string and
    fragment, either of which can carry a token. Never raises: a URL that
    cannot be parsed is reported as unparseable rather than echoed back.
    """
    text = str(url or "")
    if not text:
        return text
    try:
        parts = urlsplit(text)
        host = parts.hostname
        if not host:
            return "<unparseable url>"
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return "<unparseable url>"


def _known_secrets():
    """
    The credential values this process actually holds.

    A deny-list of real values is reliable in a way that pattern matching is
    not: we know exactly what must never appear in output.
    """
    try:
        from .config import settings
    except Exception:
        return ()

    values = [
        getattr(settings, "GRAPHQL_API_KEY", ""),
        getattr(settings, "ARIZE_DEVELOPER_API_KEY", ""),
        getattr(settings, "ARIZE_API_KEY", ""),
        getattr(settings, "OPENAI_API_KEY", ""),
    ]

    # Anything embedded in the endpoint URL: userinfo and query values.
    endpoint = getattr(settings, "GRAPHQL_ENDPOINT", "") or ""
    if endpoint:
        try:
            parts = urlsplit(endpoint)
            values.extend([parts.username or "", parts.password or ""])
            if parts.query:
                for pair in parts.query.split("&"):
                    _, _, value = pair.partition("=")
                    values.append(value)
        except ValueError:
            pass

    # And anything in explicitly configured headers.
    raw_headers = getattr(settings, "GRAPHQL_HEADERS", "") or ""
    if raw_headers and raw_headers != "{}":
        try:
            parsed = json.loads(raw_headers)
            if isinstance(parsed, dict):
                values.extend(str(v) for v in parsed.values())
        except (json.JSONDecodeError, TypeError):
            values.append(raw_headers)

    # Short values are too likely to appear coincidentally to be worth
    # substituting, and are not plausible credentials.
    return tuple({v for v in values if isinstance(v, str) and len(v) >= 8})


def scrub_secrets(text) -> str:
    """Replace any known credential value with a placeholder."""
    result = str(text or "")
    if not result:
        return result
    for secret in _known_secrets():
        if secret in result:
            result = result.replace(secret, "<redacted>")
    return result
