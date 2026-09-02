#!/usr/bin/env python3
"""
Configuration settings for the Text-to-GraphQL MCP Server.
This module loads environment variables from .env file and provides typed settings
classes that can be used throughout the application.
"""

import os
import json
from typing import Optional, Dict, Any
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Settings(BaseSettings):
    """
    Application settings loaded from environment variables
    """
    # API Keys
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    
    # Model Settings
    MODEL_NAME: str = os.getenv("MODEL_NAME", "gpt-4o")
    MODEL_TEMPERATURE: float = float(os.getenv("MODEL_TEMPERATURE", "0"))
    
    # API Settings
    API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))
    
    # GraphQL Settings
    GRAPHQL_ENDPOINT: str = os.getenv("GRAPHQL_ENDPOINT", "")
    GRAPHQL_API_KEY: str = os.getenv("GRAPHQL_API_KEY", "")
    GRAPHQL_HEADERS: str = os.getenv("GRAPHQL_HEADERS", "{}")
    GRAPHQL_AUTH_TYPE: str = os.getenv("GRAPHQL_AUTH_TYPE", "bearer")  # bearer, apikey, or custom
    
    # Schema cache settings
    # TTL after which the cached schema is re-introspected. The check is cheap and
    # runs per schema-dependent call; live introspection happens only when stale.
    SCHEMA_CACHE_TTL_SECONDS: int = int(os.getenv("SCHEMA_CACHE_TTL_SECONDS", "3600"))
    # Override the schema cache location (containers, read-only HOME, custom deploys).
    # Empty means $XDG_CACHE_HOME/text-to-graphql-mcp/schemas, else ~/.cache/...
    SCHEMA_CACHE_DIR: str = os.getenv("SCHEMA_CACHE_DIR", "")
    
    # Legacy support for Arize (backward compatibility)
    ARIZE_DEVELOPER_API_KEY: str = os.getenv("ARIZE_DEVELOPER_API_KEY", "")
    
    # Optional monitoring settings (Arize Phoenix, etc.)
    ARIZE_API_KEY: str = os.getenv("ARIZE_API_KEY", "")  # For monitoring/observability
    ARIZE_PROJECT_NAME: str = os.getenv("ARIZE_PROJECT_NAME", "text-to-graphql-mcp")
    SPACE_ID: str = os.getenv("SPACE_ID", "")
    
    # LangGraph Settings
    RECURSION_LIMIT: int = int(os.getenv("RECURSION_LIMIT", "10"))
    AGENT_VERSION: str = os.getenv("AGENT_VERSION", "1.0.0")
    
    def get_graphql_headers(self) -> Dict[str, Any]:
        """
        Parse and return GraphQL headers as a dictionary.
        Supports multiple authentication methods and custom headers.
        """
        headers = {}
        
        # Parse GRAPHQL_HEADERS if provided (highest priority)
        if self.GRAPHQL_HEADERS and self.GRAPHQL_HEADERS != "{}":
            try:
                headers = json.loads(self.GRAPHQL_HEADERS)
            except json.JSONDecodeError:
                # If not valid JSON, assume it's a simple Authorization header
                headers = {"Authorization": self.GRAPHQL_HEADERS}
        
        # Auto-configure authentication if no existing auth headers and API key is provided
        api_key = self.GRAPHQL_API_KEY or self.ARIZE_DEVELOPER_API_KEY  # Support legacy
        
        if api_key and "Authorization" not in headers and "X-API-Key" not in headers:
            # Auto-detect Arize endpoint and use appropriate auth (before general auth)
            if self.GRAPHQL_ENDPOINT and "arize.com" in self.GRAPHQL_ENDPOINT:
                # Arize specifically requires X-API-Key
                headers["X-API-Key"] = api_key
            else:
                # Use configured auth type for other APIs
                auth_type = self.GRAPHQL_AUTH_TYPE.lower()
                
                if auth_type == "bearer":
                    # Standard Bearer token (most common)
                    headers["Authorization"] = f"Bearer {api_key}"
                elif auth_type == "apikey":
                    # X-API-Key header (used by some APIs like Arize)
                    headers["X-API-Key"] = api_key
                elif auth_type == "direct":
                    # Direct authorization header
                    headers["Authorization"] = api_key
                else:
                    # Default to Bearer for unknown types
                    headers["Authorization"] = f"Bearer {api_key}"
        
        # Ensure Content-Type is set
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
            
        return headers
    
    class Config:
        env_file = ".env"
        extra = "ignore"

# Create a global settings instance
settings = Settings()

# Function to get settings for tests with potential overrides
def get_settings() -> Settings:
    """
    Returns application settings, allowing for potential overrides in tests
    """
    return settings 