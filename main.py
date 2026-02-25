from dotenv import load_dotenv 
import os 
import streamlit as st
import networkx as nx
from openai import OpenAI
from neo4j import GraphDatabase

load_dotenv()

#  PAGE CONFIG 
st.set_page_config(page_title="Windchill Actionable AI", layout="centered")

#  CONFIGURATION & CLIENTS 
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")


client = OpenAI(api_key=OPENAI_API_KEY)


# NEO4J MANAGER 
class Neo4jManager:
    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def push_graph(self, G):
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
            for node_id, node_data in G.nodes(data=True):
                session.run("""
                MERGE (n:Step {name: $id})
                SET n.type = $type, n.content = $content
                """, id=node_id, type=node_data.get('type', 'Unknown'), content=node_data.get('content', 'None'))

            for source, target, edge_data in G.edges(data=True):
                session.run("""
                MATCH (source:Step {name: $source_id})
                MATCH (target:Step {name: $target_id})
                MERGE (source)-[r:TRANSITION]->(target)
                SET r.label = $label, r.condition = $condition
                """, source_id=source, target_id=target, 
                     label=edge_data.get('label', 'None'), condition=edge_data.get('condition', 'None'))

    def retrieve_context(self, keyword):
        with self.driver.session() as session:
            query = """
            MATCH path = (n:Step)-[:TRANSITION*1..3]-(m:Step)
            WHERE 
                toLower(replace(n.name, '_', ' ')) CONTAINS toLower(replace($kw, '_', ' ')) 
                OR 
                toLower(coalesce(n.content, '')) CONTAINS toLower($kw)
            UNWIND relationships(path) as r
            WITH startNode(r) as src, r, endNode(r) as tgt
            RETURN DISTINCT src.name AS source, r.label AS label, r.condition AS cond, 
                            tgt.name AS target, 
                            src.content AS src_content, tgt.content AS tgt_content
            LIMIT 25
            """
            result = session.run(query, kw=keyword)
            
            context_list = []
            for record in result:
                link_desc = f"[{record['source']}] --({record['label']} | Cond: {record['cond']})--> [{record['target']}]"
                src_val, tgt_val = record.get('src_content'), record.get('tgt_content')
                
                if src_val and str(src_val).strip() != 'None':
                    link_desc += f"\n    |_ [{record['source']}] Payload: {str(src_val).strip()}"
                if tgt_val and str(tgt_val).strip() != 'None':
                    link_desc += f"\n    |_ [{record['target']}] Payload: {str(tgt_val).strip()}"
                    
                context_list.append(link_desc)
            return "\n\n".join(context_list) if context_list else "No relevant database context found."

def extract_search_entity(user_question):
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Extract the single most important noun, step name, or property from the user's question. Output ONLY that word. Example: 'What is wt.maxDbConnections?' -> 'wt.maxDbConnections'"},
            {"role": "user", "content": user_question}
        ],
        temperature=0.0
    )
    return response.choices[0].message.content.strip()

def import_from_json(json_string):
    """Converts a JSON string back into a NetworkX graph."""

    data = json.loads(json_string)
    
    graph = json_graph.node_link_graph(data)
    
    return graph

def generate_jira_ticket(chat_history):
    """Summarizes the chat history into a professional Jira ticket."""
    # Filter out system prompts and raw RAG blocks for cleaner summarization
    clean_history = []
    for msg in chat_history:
        if msg["role"] == "user" and not msg["content"].startswith("Database Context:"):
            clean_history.append(f"User: {msg['content']}")
        elif msg["role"] == "assistant" and "🎟️" not in msg["content"]:
            clean_history.append(f"Agent: {msg['content']}")
            
    convo_text = "\n".join(clean_history)
    
    prompt = f"""
    Based on the following technical support chat, generate a professional Jira Bug/Support Ticket in Markdown format.
    Include the following sections:
    - **Summary** (A concise title)
    - **Description / Symptoms** (What the user is experiencing)
    - **Diagnostic Steps Taken** (Summarize the files checked and CLI commands generated)
    - **Recommended Action / Resolution**
    
    Chat Log:
    {convo_text}
    """
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a Technical Support Manager documenting a Jira ticket."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )
    return response.choices[0].message.content

SYSTEM_PROMPT = """
You are a Senior Tech Support Engineer for PTC Windchill. 
Use the provided Database Context to answer accurately. 

CRITICAL RULES FOR ACTIONABLE RAG:
1. If the Database Context mentions a specific file path (e.g., WT_HOME/codebase/wt.properties), you MUST generate the exact Linux bash commands to navigate to or check that file.
2. If the Database Context mentions a specific property (e.g., wt.maxDbConnections), you MUST generate a bash command (like 'grep' or 'cat') that the user can copy-paste to verify that property's current value on their server.
3. Always format your CLI commands inside markdown bash code blocks.
4. Assume standard environment variables like $WT_HOME and $TOMCAT_HOME are set.
"""

if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

#  SIDEBAR UTILITIES 
with st.sidebar:
    st.header("⚙️ Admin Tools")
    if st.button("🔄 Sync Database to Neo4j"):
        with st.spinner("Pushing graph to database..."):
            db = Neo4jManager(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
            import json
            # G = build_flowchart_graph()
            with open("flowchart.json", "r", encoding="utf-8") as f: 
                raw_json = json.load(f)

            dat = json.loads(raw_json)
            
            db.push_graph(import_from_json(dat))
            st.success("Database synced successfully!")
            
    st.divider()
    st.header("📝 Documentation")
    if st.button("Generate Jira Ticket"):
        if len(st.session_state.messages) > 1:
            with st.spinner("Writing Jira Ticket..."):
                ticket_markdown = generate_jira_ticket(st.session_state.messages)
                # Inject the ticket directly into the chat history
                st.session_state.messages.append({"role": "assistant", "content": f"**🎟️ Generated Jira Ticket:**\n\n{ticket_markdown}"})
            st.rerun()
        else:
            st.warning("Chat history is empty. Ask some questions first!")

    st.divider()
    if st.button("🗑️ Clear Chat History"):
        st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        st.rerun()

#  MAIN CHAT INTERFACE 
st.title("💻 Windchill Actionable Support Copilot")
st.caption("Ask a diagnostic question. The AI will query Neo4j, show you the data, and generate CLI commands.")

# Display chat history
for msg in st.session_state.messages:
    if msg["role"] != "system":
        # We handle displaying the raw context differently now so we skip the injected "Database Context:" logs in the UI
        if msg["role"] == "user" and msg["content"].startswith("Database Context:"):
            continue 
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# Chat Input Trigger
if user_input := st.chat_input("e.g., 'What should wt.pom.rowPrefetchCount be set to?'"):
    
    # Display user message
    with st.chat_message("user"):
        st.markdown(user_input)
    
    # Extract keyword & Fetch Context
    keyword = extract_search_entity(user_input)
    db = Neo4jManager(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    context = db.retrieve_context(keyword)
    
    # Formulate hidden backend prompt for the LLM
    rag_prompt = f"Database Context:\n{context}\n\nUser Question: {user_input}"
    temp_messages = st.session_state.messages + [{"role": "user", "content": rag_prompt}]
    
    #  Generate Answer & Display UI
    with st.chat_message("assistant"):
        # Explicitly display the DB query process first
        st.info(f"**🔍 Neo4j Query Executed**\n* **Extracted Entity:** `{keyword}`\n* **Retrieved Graph Data:**\n```text\n{context}\n```")
        
        with st.spinner("Generating actionable diagnostic scripts..."):
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=temp_messages,
                temperature=0.3
            )
            answer = response.choices[0].message.content
            st.markdown(answer)
    
    # Save to history
    # We save the prompt with the context so the LLM remembers the graph data later
    st.session_state.messages.append({"role": "user", "content": rag_prompt}) 
    st.session_state.messages.append({"role": "assistant", "content": answer})