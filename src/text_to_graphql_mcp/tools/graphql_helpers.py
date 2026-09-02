from graphql.utilities import print_type
from typing import Union
from graphql.type import (
    GraphQLObjectType,
    GraphQLUnionType,
    GraphQLWrappingType,
    GraphQLNamedType,
    GraphQLScalarType,
    GraphQLEnumType,
    GraphQLSchema
)
from collections import OrderedDict
from ..logger import logger
from ..config import settings
from gql.transport.httpx import HTTPXTransport
from gql import Client as GQLClient, gql

#Global variable to store all relevant types, used when finding all relevant GQL types for a given root type
#The recursive nature of get_all_relevant_types() was making it difficult to track all of the types that had already been seen and resulting in circular dependencies causing issues
all_relevant_types_glob = []

def get_argument_type(schema, arg):
    arg_type = arg.type
    if isinstance(arg_type, GraphQLWrappingType):
        arg_type = arg_type.of_type
        arg_name = arg_type.name
    elif isinstance(arg_type, GraphQLNamedType):
        arg_name = arg_type.name
    return arg_name

def ordered_set_from_list(input_list):
    return list(OrderedDict.fromkeys(input_list))

def get_mutation_type_str(schema, type_name):
    return print_type(schema.get_type(type_name))

def unwrap_type(type_obj: GraphQLWrappingType):
    #Some types may be wrapped multiple times (for example: [String!]! is a GraphQLList wrapped in GraphQLNonNullable)
    field_type = type_obj.of_type
    while isinstance(field_type, GraphQLWrappingType):
        field_type = field_type.of_type
    return field_type

def get_field_types(schema, field):
    field_type = field.type
    if isinstance(field_type, GraphQLUnionType):
        field_types = []
        for t in field_type.types:
            for subfield in t.fields.values():
                field_types += get_field_types(schema, subfield)
        return field_types
    elif isinstance(field_type, GraphQLWrappingType):
        field_type = unwrap_type(field_type)


    return [field_type.name]


def get_all_relevant_types(schema: GraphQLSchema, type_obj: Union[str, GraphQLObjectType], recursive: bool = True):
    global all_relevant_types_glob
    relevant_types = []
    root_type = None

    if isinstance(type_obj, str):
        root_type = schema.get_type(type_obj)
    else:
        root_type = type_obj
    if root_type is None:
        return []
    if isinstance(root_type, GraphQLScalarType) or isinstance(root_type, GraphQLEnumType):
        return []
    
    root_types = []
    if isinstance(root_type, GraphQLUnionType):
        for t in root_type.types:
            relevant_types.append(t.name)
            root_types.append(t)
    else:
        root_types.append(root_type)

    for root_type in root_types:
        for field in root_type.fields.values():
            relevant_types += get_field_types(schema, field)

    if not recursive:
        return relevant_types

    new_types = []
    node = schema.get_type("Node")
    for t in relevant_types:
        is_node_type = schema.get_type(t) in schema.get_implementations(node).objects
        if t not in all_relevant_types_glob and not is_node_type:
            new_types.append(t)
    
    all_relevant_types_glob += relevant_types
    all_relevant_types_glob = ordered_set_from_list(all_relevant_types_glob)

    for t in new_types:
        get_all_relevant_types(schema, t)

# Too many descriptions add so many tokens :(
def remove_graphql_descriptions(schema_string):
    """
    Removes all descriptions (text enclosed in triple quotes) from a GraphQL schema string.
    
    Args:
        schema_string (str): The GraphQL schema string
        
    Returns:
        str: The schema string with all descriptions removed
    """
    lines = schema_string.split('\n')
    result_lines = []
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Check if line contains triple quotes
        if '"""' in line:
            # Check if opening and closing quotes are on the same line
            if line.count('"""') == 2:
                # Remove the description part only
                parts = line.split('"""')
                cleaned_line = parts[0] + parts[2]
                result_lines.append(cleaned_line)
            else:
                # This is the start of a multi-line description
                # Skip lines until we find the closing triple quotes
                j = i + 1
                while j < len(lines) and '"""' not in lines[j]:
                    j += 1
                
                # Skip to the line after the closing quotes
                if j < len(lines):
                    i = j  # Move to the line with closing quotes
                else:
                    break  # Shouldn't happen with valid schema
        else:
            result_lines.append(line)
        
        i += 1
    
    return '\n'.join(result_lines).replace("\n  \n", "\n").replace("\n\n", "\n").replace("\n    \n", "\n")

def build_type_context_string(schema, context_types, root_type):
    context_str = ""
    for c_type in context_types:
        schema_type = schema.get_type(c_type)
        schema_type_str = print_type(schema_type)
        #Only keep the descriptions for the root type and the fields on that type
        if c_type not in get_all_relevant_types(schema, root_type, recursive=False) and c_type != root_type:
            schema_type_str = remove_graphql_descriptions(schema_type_str) + "\n\n"
        context_str += schema_type_str

    return context_str

def construct_mutation_context(schema, mutation_name):
    global all_relevant_types_glob
    mutation = schema.get_type('Mutation')
    mutation_field = mutation.fields[mutation_name]
    arg = list(mutation_field.args.values())[0]
    input_type = get_argument_type(schema, arg)
    response_type = mutation_field.type.name
    
    mutation_context = """type Mutation {\n"""
    mutation_context += f"  {mutation_name}(input: {input_type}!): {response_type}\n"
    mutation_context += "}\n\n"
    
    context_types = [input_type, response_type]
    all_relevant_types_glob.clear()
    all_relevant_types_glob.append(input_type)
    all_relevant_types_glob.append(response_type)

    get_all_relevant_types(schema, input_type)
    get_all_relevant_types(schema, response_type)
    context_types = all_relevant_types_glob

    mutation_context += build_type_context_string(schema, context_types, mutation_name)

    return mutation_context

def construct_query_context(schema, query_name):
    global all_relevant_types_glob

    all_relevant_types_glob.clear()
    all_relevant_types_glob.append("Query")
    all_relevant_types_glob.append(query_name)
    get_all_relevant_types(schema, query_name)
    context_types = all_relevant_types_glob

    query_context = build_type_context_string(schema, context_types, query_name)

    #Would be nice to have the descriptions for the prompt, but for example with the Model query, this cuts it from 17k tokens to 6k
    query_context = remove_graphql_descriptions(query_context)
    query_context = query_context.replace("\n  \n", "\n").replace("\n\n", "\n").replace("\n    \n", "\n")

    return query_context

def execute_graphql_query(endpoint_url, query, headers=None, variables=None, operation_name=None, timeout=None):
    """
    Execute a GraphQL query against a specified endpoint.
    
    Args:
        endpoint_url: The GraphQL API endpoint URL
        query: The GraphQL query or mutation string
        headers: Optional headers for the request (e.g., authentication)
        variables: Optional variables for the GraphQL query
        operation_name: Optional operation name for the GraphQL query
        timeout: Optional request timeout in seconds. None keeps httpx's default.
        
    Returns:
        Dict containing the response data or error information
    """        
    
    try:
        logger.info(f"Executing GraphQL query against: {endpoint_url}")
        
        # Use configured headers if none provided
        if headers is None:
            request_headers = settings.get_graphql_headers()
            logger.info("Using configured authentication headers from settings")
        else:
            # Start with basic headers and add custom ones
            request_headers = {"Content-Type": "application/json"}
            request_headers.update(headers)
        
        # Use configured endpoint if not provided or if provided endpoint is empty
        if not endpoint_url or endpoint_url.strip() == "":
            endpoint_url = settings.GRAPHQL_ENDPOINT
            logger.info(f"Using configured GraphQL endpoint: {endpoint_url}")
        
        transport_kwargs = {"url": endpoint_url, "headers": request_headers}
        if timeout is not None:
            transport_kwargs["timeout"] = timeout
        transport = HTTPXTransport(**transport_kwargs)
        
        # Create GQL client with transport
        client = GQLClient(transport=transport, fetch_schema_from_transport=False)


        try:
            # Parse and execute query
            result = client.execute(gql(query), variable_values=variables)

            
            logger.info("GraphQL query executed successfully")
            return {
                "success": True,
                "data": result
            }
        except Exception as e:
            logger.error(f"Error executing GraphQL query: {str(e)}")
            return {
                "success": False,
                "message": str(e),
                "exception": type(e).__name__
            }
    except Exception as e:
        logger.error(f"Error setting up GraphQL client: {str(e)}")
        return {
            "success": False,
            "message": f"Client setup error: {str(e)}",
            "exception": type(e).__name__
        }