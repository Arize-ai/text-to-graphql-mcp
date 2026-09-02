#!/usr/bin/env python3
"""
Text-to-GraphQL Agent Implementation using LangGraph.
This file contains the implementation of the agent that processes natural language
queries into GraphQL queries, validates them, executes them, and visualizes the results.
"""

from typing import Dict, Any, List, Optional, TypedDict, Literal, cast, Union
import json
import re

from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.language_models import BaseChatModel

from .config import settings
from .logger import logger
from .types import AgentState

# Import tools
from .tools import (
    intent_recognition,
    schema_management,
    intent_options,
    select_options,
    query_construction,
    query_validation,
    query_execution,
    data_visualization,
    query_optimization
)

# Initialize LLM
llm = ChatOpenAI(
    model=settings.MODEL_NAME,
    temperature=settings.MODEL_TEMPERATURE,
    api_key=settings.OPENAI_API_KEY
)

# Tool definitions
tools = [
    {
        "name": "intent_recognition",
        "description": "Analyzes natural language queries to identify GraphQL intent (query vs mutation)",
        "when_to_use": "Always use as the first step of processing a new user query to determine what the user is trying to accomplish",
        "prerequisites": "Natural language query",
        "provides": "Intent information (query/mutation)"
    },
    {
        "name": "schema_management",
        "description": "Manages GraphQL schema and identifies relevant schema types for the user's query",
        "when_to_use": "After intent recognition to identify the relevant parts of the GraphQL schema",
        "prerequisites": "Intent information",
        "provides": "Relevant schema types and their fields"
    },
    {
        "name": "intent_options",
        "description": "Gets the available query options based on the user's intent and schema",
        "when_to_use": "After schema management to generate a list of possible GraphQL operations",
        "prerequisites": "Intent information and schema",
        "provides": "List of possible operations"
    },
    {
        "name": "select_options",
        "description": "Selects the most appropriate GraphQL operation from the available options",
        "when_to_use": "After intent_options to select the best option for the user's query",
        "prerequisites": "List of options from intent_options",
        "provides": "Selected GraphQL operation"
    },
    {
        "name": "query_construction",
        "description": "Builds executable GraphQL queries from intent and selected option",
        "when_to_use": "After selecting an option to construct the actual GraphQL query",
        "prerequisites": "Selected GraphQL operation",
        "provides": "GraphQL query string"
    },
    {
        "name": "query_validation",
        "description": "Validates GraphQL queries for correctness and optimization",
        "when_to_use": "After query construction to ensure the query is valid",
        "prerequisites": "GraphQL query string",
        "provides": "Validation result and possibly an improved query"
    },
    {
        "name": "query_optimization",
        "description": "Optimizes GraphQL queries based on validation errors",
        "when_to_use": "After query validation if the query is invalid and needs improvement",
        "prerequisites": "GraphQL query and validation errors",
        "provides": "Improved GraphQL query"
    },
    {
        "name": "query_execution",
        "description": "Executes GraphQL queries against the API endpoint",
        "when_to_use": "After query validation if the query is valid",
        "prerequisites": "Valid GraphQL query",
        "provides": "Query execution results"
    },
    {
        "name": "data_visualization",
        "description": "Recommends visualizations for query results",
        "when_to_use": "After query execution if there is data to visualize",
        "prerequisites": "Query execution results",
        "provides": "Visualization recommendations and configurations"
    }
]

# Function implementations
tool_functions = {
    "intent_recognition": intent_recognition,
    "schema_management": schema_management,
    "intent_options": intent_options,
    "select_options": select_options,
    "query_construction": query_construction,
    "query_validation": query_validation,
    "query_optimization": query_optimization,
    "query_execution": query_execution,
    "data_visualization": data_visualization
}

# LLM tool selector
def get_tool_selector_prompt() -> ChatPromptTemplate:
    """Returns the prompt for the LLM tool selector"""
    template = """You are the controller for a GraphQL agent. Your task is to determine which tool the agent should use next based on the current state of its execution.

## Available Tools
{tools}

## Current Agent State
{state}

## Guidelines
- Analyze the current state to determine what information the agent has and what it needs next
- Consider prerequisites for each tool and whether they're satisfied
- Select the most appropriate tool for the current state
- If the workflow is complete or has encountered an error that can't be resolved, select "END"
- Provide a confidence score between 0 and 1 indicating your certainty in this decision
  - 0.0-0.3 for low confidence (unsure which tool to use)
  - 0.4-0.7 for medium confidence (reasonable guess)
  - 0.8-1.0 for high confidence (clear choice)

## Response Format
Respond with a JSON object with three fields:
1. "tool": The name of the tool to use next (one of: "intent_recognition", "schema_management", "intent_options", "select_options", "query_construction", "query_validation", "query_execution", "data_visualization", or "END")
2. "reasoning": A brief explanation of why this tool is appropriate at this stage
3. "confidence": A number between 0 and 1 indicating your confidence in this selection

Example response:
```json
{
  "tool": "schema_management",
  "reasoning": "Intent recognition has been completed, so now we need to identify the relevant schema types.",
  "confidence": 0.9
}
```
"""
    return ChatPromptTemplate.from_template(template)

async def tool_selector(state: AgentState) -> Dict[str, Any]:
    """Decides which tool to use next based on the current state"""
    try:
        # Get the LLM from state
        agent_llm = state.get("default_llm")
        if not agent_llm:
            logger.warning("No default LLM provided for tool selection")
            return {"tool": "END", "reasoning": "No LLM available for tool selection", "confidence": 0.0}
            
        # Format the state for the prompt
        # Remove large data structures and LLM object that might cause prompt size issues
        state_for_prompt = {k: v for k, v in state.items() if k not in ["default_llm", "messages"]}
        
        # If there's execution_result data, summarize it instead of including all of it
        if "execution_result" in state_for_prompt:
            if isinstance(state_for_prompt["execution_result"], dict):
                keys = list(state_for_prompt["execution_result"].get("data", {}).keys())
                state_for_prompt["execution_result"] = {
                    "status": state_for_prompt["execution_result"].get("status", "UNKNOWN"),
                    "data_keys": keys,
                    "has_data": bool(state_for_prompt["execution_result"].get("data"))
                }
        
        # Similarly, summarize visualization results
        if "visualization_result" in state_for_prompt:
            viz_result = state_for_prompt["visualization_result"]
            if isinstance(viz_result, dict):
                viz_count = len(viz_result.get("visualizations", []))
                viz_types = [v.get("type") for v in viz_result.get("visualizations", [])]
                state_for_prompt["visualization_result"] = {
                    "visualization_count": viz_count,
                    "visualization_types": viz_types,
                    "has_recommendations": bool(viz_result.get("recommendations"))
                }
        
        # Get current step info
        current_step = state.get("current_step", "start")
        
        # Function to make rule-based decisions with high confidence
        def rule_based_decision() -> Dict[str, Any]:
            # Simple rule-based decisions based on workflow state to handle common transitions
            if current_step == "intent_recognition" and state.get("intent"):
                return {"tool": "schema_management", "reasoning": "Intent recognized, moving to schema management", "confidence": 0.95}
                
            if current_step == "schema_management" and state.get("schema"):
                return {"tool": "intent_options", "reasoning": "Schema retrieved, getting intent options", "confidence": 0.95}
                
            if current_step == "intent_options" and state.get("options"):
                return {"tool": "select_options", "reasoning": "Options retrieved, selecting the best option", "confidence": 0.95}
                
            if current_step == "select_options" and state.get("selected_option"):
                return {"tool": "query_construction", "reasoning": "Option selected, constructing query", "confidence": 0.95}
                
            if current_step == "query_construction" and state.get("query"):
                return {"tool": "query_validation", "reasoning": "Query constructed, validating", "confidence": 0.95}
                
            if current_step == "query_validation":
                validation_result = state.get("query_validation_result", {}).get("is_valid", False)
                if validation_result and state.get("query"):
                    # return {"tool": "query_execution", "reasoning": "Query validated, executing", "confidence": 0.95}
                    # Going to let the execute_query endpoint handle this instead of doing it automatically here
                    return {"tool": "END", "reasoning": "Query validated, returning results", "confidence": 0.95}

                else:
                    # Check if we should try optimization
                    optimization_attempts = state.get("optimization_attempts", 0)
                    max_optimization_attempts = 3
                    if optimization_attempts < max_optimization_attempts and validation_result is False:
                        return {"tool": "query_optimization", "reasoning": "Query validation failed, attempting optimization", "confidence": 0.95}
                    else:
                        return {"tool": "END", "reasoning": "Query validation failed and max optimization attempts reached", "confidence": 0.9}
            
            if current_step == "query_optimization" and state.get("query"):
                return {"tool": "query_validation", "reasoning": "Query optimized, validating again", "confidence": 0.95}
                
            if current_step == "query_execution" and state.get("execution_result", {}).get("data"):
                return {"tool": "data_visualization", "reasoning": "Query executed, visualizing results", "confidence": 0.95}
                
            if current_step == "data_visualization" and state.get("visualization_result"):
                return {"tool": "END", "reasoning": "Visualization complete, ending workflow", "confidence": 0.95}
                
            # If no rule matches, return with low confidence to indicate LLM should make decision
            return None
        
        # First, try to make a rule-based decision for standard transitions
        rule_decision = rule_based_decision()
        
        # If we have a high-confidence rule-based decision, use it without consulting the LLM
        if rule_decision:
            return rule_decision
            
        # If we don't have a clear rule, consult the LLM
        # Use a more concise version of tools for the prompt
        tools_for_prompt = []
        for tool in tools:
            tools_for_prompt.append(
                f"- {tool['name']}: {tool['description']}\n  When to use: {tool['when_to_use']}\n  Prerequisites: {tool['prerequisites']}\n"
            )
        
        # Get the tool selector prompt
        prompt = get_tool_selector_prompt().format(
            tools="\n".join(tools_for_prompt),
            state=json.dumps(state_for_prompt, indent=2, default=str)
        )
        
        # Call the LLM to select the next tool
        response = await agent_llm.ainvoke(prompt)
        response_content = response.content
        
        # Parse the response
        logger.info(f"Tool selector response: {response_content}")
        
        # Extract JSON from the response
        try:
            # First try: Look for JSON code blocks
            json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response_content)
            if json_match:
                json_str = json_match.group(1).strip()
                result = json.loads(json_str)
            else:
                # Second try: Try to parse the whole response
                result = json.loads(response_content.strip())
                
            # Ensure confidence value exists and is a float between 0 and 1
            if "confidence" not in result:
                result["confidence"] = 0.5  # Default medium confidence
            else:
                try:
                    confidence = float(result["confidence"])
                    # Ensure confidence is in valid range
                    result["confidence"] = max(0.0, min(1.0, confidence))
                except:
                    result["confidence"] = 0.5  # Default if conversion fails
            
            logger.info(f"Selected tool: {result.get('tool')} - Confidence: {result.get('confidence')} - Reasoning: {result.get('reasoning')}")
            
            # Only use LLM judgment if confidence is at least 0.4
            if result.get("confidence", 0) >= 0.4:
                return result
            else:
                logger.info(f"Low LLM confidence ({result.get('confidence')}), falling back to rule-based decision")
                # Try rule-based decision again
                fallback = rule_based_decision()
                if fallback:
                    return fallback
                
                # If no rule applies, use the LLM decision anyway, but with a note
                result["reasoning"] = f"Using LLM decision despite low confidence: {result.get('reasoning')}"
                return result
                
        except json.JSONDecodeError:
            # If we can't parse JSON, look for tool name in the response text
            logger.warning("Failed to parse JSON from LLM response, extracting tool name from text")
            
            # Try to extract confidence value from text
            confidence_match = re.search(r'["\']confidence["\']\s*:\s*(0\.\d+|1\.0|1)', response_content)
            confidence = 0.3  # Default low confidence when parsing fails
            if confidence_match:
                try:
                    confidence = float(confidence_match.group(1))
                except:
                    pass
            
            # Check for exact tool names in the response
            for tool_name in tool_functions.keys():
                if f'"tool": "{tool_name}"' in response_content or f"'tool': '{tool_name}'" in response_content:
                    return {"tool": tool_name, "reasoning": "Extracted from response text", "confidence": confidence}
            
            # Look for END keyword
            if '"tool": "END"' in response_content or "'tool': 'END'" in response_content:
                return {"tool": "END", "reasoning": "Extracted END from response text", "confidence": confidence}
                
            # Since we have parsing issues and low confidence, fall back to rule-based
            fallback = rule_based_decision()
            if fallback:
                return fallback
                
            # Try to guess the next logical step based on state
            if state.get("intent") and not state.get("schema"):
                return {"tool": "schema_management", "reasoning": "Logical next step after intent recognition", "confidence": 0.7}
            elif state.get("schema") and not state.get("options"):
                return {"tool": "intent_options", "reasoning": "Logical next step after schema management", "confidence": 0.7}
            elif state.get("options") and not state.get("selected_option"):
                return {"tool": "select_options", "reasoning": "Logical next step after getting options", "confidence": 0.7}
            elif state.get("selected_option") and not state.get("query"):
                return {"tool": "query_construction", "reasoning": "Logical next step after selecting option", "confidence": 0.7}
            elif state.get("query") and not state.get("query_validation_result"):
                return {"tool": "query_validation", "reasoning": "Logical next step after query construction", "confidence": 0.7}
            # elif state.get("query_validation_result", {}).get("is_valid") and not state.get("execution_result"):
            #     return {"tool": "query_execution", "reasoning": "Logical next step after validation", "confidence": 0.7}
            elif state.get("execution_result") and not state.get("visualization_result"):
                return {"tool": "data_visualization", "reasoning": "Logical next step after execution", "confidence": 0.7}
            else:
                return {"tool": "END", "reasoning": "Cannot determine next step, ending workflow", "confidence": 0.5}
                
    except Exception as e:
        logger.error(f"Error in tool selector: {str(e)}", exc_info=True)
        # Make a best guess at the next tool based on current state
        current_step = state.get("current_step", "start")
        
        # Simple fallback logic for common transitions
        if current_step == "intent_recognition":
            return {"tool": "schema_management", "reasoning": "Fallback to schema management after intent recognition", "confidence": 0.6}
        elif current_step == "schema_management":
            return {"tool": "intent_options", "reasoning": "Fallback to intent options after schema management", "confidence": 0.6}
        elif current_step == "intent_options":
            return {"tool": "select_options", "reasoning": "Fallback to select options after intent options", "confidence": 0.6}
        elif current_step == "select_options":
            return {"tool": "query_construction", "reasoning": "Fallback to query construction after select options", "confidence": 0.6}
        elif current_step == "query_construction":
            return {"tool": "query_validation", "reasoning": "Fallback to query validation after construction", "confidence": 0.6}
        else:
            return {"tool": "END", "reasoning": f"Error in tool selector: {str(e)}. Ending workflow.", "confidence": 0.5}

# Agent controller (decides which tool to use next)
async def agent_controller(state: AgentState) -> Literal[
    "intent_recognition", "schema_management", "intent_options", 
    "select_options", "query_construction", "query_validation", 
    "query_optimization", "query_execution", "data_visualization", END
]:
    """Determines the next step in the agent's workflow using the tool selector"""
    
    # Check for tool errors
    if state.get("tool_error"):
        logger.error(f"Tool error encountered: {state['tool_error']}")
        return END
    
    # If this is the start, always begin with intent recognition
    current_step = state.get("current_step", "start")
    if current_step == "start":
        return "intent_recognition"
    
    # Otherwise, use the tool selector to determine the next step
    selector_result = await tool_selector(state)
    next_tool = selector_result.get("tool")
    confidence = selector_result.get("confidence", 0.0)
    
    # Log the selection with confidence
    logger.json("INFO", "Tool selection", {
        "current_step": current_step,
        "selected_tool": next_tool,
        "confidence": confidence,
        "reasoning": selector_result.get("reasoning")
    })
    
    # Return the selected tool or END
    if next_tool == "END":
        return END
    elif next_tool in tool_functions:
        return cast(
            Literal["intent_recognition", "schema_management", "intent_options", 
                   "select_options", "query_construction", "query_validation", 
                   "query_optimization", "query_execution", "data_visualization"],
            next_tool
        )
    else:
        logger.warning(f"Unknown tool selected: {next_tool}, ending workflow")
        return END

# Create a graph for the agent workflow
def build_graph() -> StateGraph:
    """Build the workflow graph for the GraphQL agent"""
    
    workflow = StateGraph(AgentState)
    
    # Add nodes for each step in the workflow
    for tool_name, tool_function in tool_functions.items():
        workflow.add_node(tool_name, tool_function)
    
    # Add a specific start node to resolve the empty string issue
    workflow.add_node("start", lambda x: x)
    
    # Add conditional edges using the async controller
    for node_name in list(tool_functions.keys()) + ["start"]:
        workflow.add_conditional_edges(node_name, agent_controller)
    
    # Set entry point to the start node
    workflow.set_entry_point("start")
    
    logger.json("INFO", "Flexible agent workflow graph built", {
        "nodes": ["start"] + list(tool_functions.keys()),
        "entry_point": "start",
        "recursion_limit": settings.RECURSION_LIMIT
    })
    
    return workflow

# Main GraphQL Agent class
class GraphQLAgent:
    """LangGraph-based GraphQL Agent with flexible workflow"""
    
    def __init__(self):
        """Initialize the GraphQL Agent"""
        self.graph = build_graph().compile()
        logger.info("Flexible GraphQL Agent initialized")
        self.llm = ChatOpenAI(
            model=settings.MODEL_NAME,
            temperature=settings.MODEL_TEMPERATURE,
            api_key=settings.OPENAI_API_KEY
        )
        
        logger.json("INFO", "LLM configuration", {
            "model": settings.MODEL_NAME,
            "temperature": settings.MODEL_TEMPERATURE,
            "tracing_enabled": False
        })

        self.state = {
            "default_llm": self.llm,
            "current_step": "start",
            "messages": []
        }
    
    async def generate_query(self, natural_language_query: str, session_id: str) -> Dict[str, Any]:
        """Generate a GraphQL query from natural language"""
        logger.llm("Processing natural language query", prompt=natural_language_query)

        # Workaround due to new queries automatically pulling up previous query result instead of generating a new one
        # Probably need a better solution in the future
        self.state = {
            "default_llm": self.llm,
            "current_step": "start",
            "messages": []
        }

        # Initialize the state for query generation flow
        self.state['messages'].append(HumanMessage(content=natural_language_query))
        self.state['natural_language_query'] = natural_language_query
        # Reset optimization attempts for a new query
        self.state['optimization_attempts'] = 0
        # Execute the graph
        result = await self.graph.ainvoke(self.state)
        self.state = result
        
        # Extract relevant information
        response = {
            "graphql_query": result.get("query"),
            "validation_result": result.get("query_validation_result")
        }
        # agent_controller ends the workflow on a tool error but only logged it,
        # so the caller saw a null query with no explanation. Surface it.
        if result.get("tool_error"):
            response["error"] = result["tool_error"]
        
        logger.json("INFO", "Query generation completed", response)
        return response

    async def validate_query(self, graphql_query: str, natural_language_query: str, session_id) -> Dict[str, Any]:
        """Validate a GraphQL query"""
        logger.json("INFO", "Validating GraphQL query", {
            "graphql_query": graphql_query[:100] + ("..." if len(graphql_query) > 100 else ""),
            "natural_language_query": natural_language_query
        })
        
        self.state['messages'].append(HumanMessage(content=f"Validate this GraphQL query: {graphql_query}"))
        self.state['natural_language_query'] = natural_language_query
        self.state['query'] = graphql_query
        
        # Execute just the validation step
        result = await query_validation(self.state)
        self.state.update(result)
        
        # Return validation result
        validation_result = result.get("query_validation_result", {})
        logger.json("INFO", "Query validation result", validation_result)
        return validation_result

    async def execute_query(self, graphql_query: str, variables: Dict[str, Any] = None) -> Dict[str, Any]:
        """Execute a GraphQL query"""
        logger.json("INFO", "Executing GraphQL query", {
            "graphql_query": graphql_query[:100] + ("..." if len(graphql_query) > 100 else "")
        })
        
        self.state['messages'].append(HumanMessage(content=f"Execute this GraphQL query: {graphql_query}"))
        self.state['query'] = graphql_query

        # Execute just the execution step
        if variables:
            result = await query_execution(self.state, variables=variables)
        else:
            result = await query_execution(self.state)
        self.state.update(result)
        
        # Return execution result
        execution_result = result.get("execution_result", {})
        return execution_result

    async def visualize_data(self, data: Dict[str, Any], natural_language_query: str) -> Dict[str, Any]:
        """Generate visualizations for query results"""
        logger.json("INFO", "Generating visualizations", {
            "data_size": len(str(data)),
            "natural_language_query": natural_language_query,
            "data_structure": list(data.keys()) if isinstance(data, dict) else "non-dict data"
        })
        
        self.state['messages'].append(HumanMessage(content=f"Visualize this data: {json.dumps(data)}"))
        self.state['execution_result'] = {"data": data, "status": "SUCCESS"}
        self.state['current_step'] = "data_visualization"

        # Execute just the visualization step
        result = await data_visualization(self.state)
        self.state.update(result)
        
        # Return visualization result
        viz_result = result.get("visualization_result", {})
        logger.json("INFO", "Visualization generated", {
            "num_visualizations": len(viz_result.get("visualizations", [])),
            "visualization_types": [v.get("type") for v in viz_result.get("visualizations", [])],
            "recommendations": viz_result.get("recommendations", []),
            "visualizations": viz_result.get("visualizations", [])
        })
        return viz_result