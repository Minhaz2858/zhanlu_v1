"""Seed script — populates the database with initial data.

Run: python seed.py

Creates:
- Admin user (admin@zhanlu.dev / admin123)
- Sample projects, MarketAgents, Tools, AgentApps, KnowledgeBases
- Sample AutomationTasks, ChatSessions, ChatMessages, Reports, DecisionFlows
- Default UserSetting
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, engine, Base
from app.models import (
    User, Project, ChatSession, ChatMessage, AgentApp, KnowledgeBase,
    AutomationTask, Tool, UserFile, Report, DecisionFlow, MarketAgent,
    McpServer, UserSetting, AgentConversation,
    AnalyticsEvent, OtpCode, PasswordResetToken,
)
from app.services.auth_service import auth_service

# Import tool_handlers to trigger registration of all new tools in the registry
import app.services.tool_handlers  # noqa: F401


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    if db.query(User).count() > 0:
        print("Database already seeded. Skipping.")
        db.close()
        return

    print("Seeding database...")

    # --- Admin User ---
    admin = User(
        email="admin@zhanlu.dev",
        full_name="Admin User",
        role="admin",
        password_hash=auth_service.hash_password("admin123"),
    )
    db.add(admin)
    db.flush()

    # --- Projects ---
    projects = [
        Project(name="Global", description="Global project for all resources", color="#3B82F6", status="active", created_by_id=admin.id),
        Project(name="Marketing Team", description="Marketing automation project", color="#10B981", status="active", created_by_id=admin.id),
        Project(name="Data Analysis", description="Data analysis and reporting", color="#F59E0B", status="active", created_by_id=admin.id),
        Project(name="Customer Support", description="Customer support automation", color="#EF4444", status="active", created_by_id=admin.id),
    ]
    db.add_all(projects)
    db.flush()

    # --- MarketAgents ---
    market_agents = [
        MarketAgent(name="Industry Research Analyst", category="industry", description="Analyzes industry trends and generates comprehensive market research reports", capabilities=["market_analysis", "trend_forecasting", "competitor_analysis"], rating=4.7, subscribers=234, created_by_id=admin.id),
        MarketAgent(name="Office Document Generator", category="office", description="Generates professional documents, spreadsheets, and presentations from templates", capabilities=["document_generation", "template_matching", "formatting"], rating=4.5, subscribers=189, created_by_id=admin.id),
        MarketAgent(name="Ops Compliance Monitor", category="ops_governance", description="Monitors operational compliance and generates audit reports", capabilities=["compliance_checking", "audit_trail", "risk_assessment"], rating=4.3, subscribers=92, created_by_id=admin.id),
        MarketAgent(name="Data Pipeline Builder", category="data_processing", description="Builds and optimizes data processing pipelines with visual flow design", capabilities=["etl_design", "data_transformation", "pipeline_optimization"], rating=4.8, subscribers=312, created_by_id=admin.id),
        MarketAgent(name="Financial Report Generator", category="office", description="Creates detailed financial reports with charts and analysis", capabilities=["financial_analysis", "chart_generation", "report_formatting"], rating=4.6, subscribers=156, created_by_id=admin.id),
        MarketAgent(name="Market Intelligence Scout", category="industry", description="Scouts market opportunities and competitive intelligence", capabilities=["opportunity_detection", "competitive_analysis", "market_sizing"], rating=4.4, subscribers=78, created_by_id=admin.id),
    ]
    db.add_all(market_agents)

    # --- Tools ---
    tools = [
        Tool(name="Web Search", description="Search the web for information", kind="system_skill", category="search", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Web Extract", description="Extract text content from web URLs", kind="system_skill", category="web", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Memory", description="Persistent agent memory across conversations", kind="system_skill", category="memory", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Todo", description="Task planning and progress tracking", kind="system_skill", category="planning", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Read File", description="Read files from the agent workspace", kind="system_skill", category="files", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Write File", description="Write files to the agent workspace", kind="system_skill", category="files", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Image Generation", description="Generate images from text prompts using AI", kind="system_skill", category="media", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Code Executor", description="Execute Python code in a sandbox", kind="system_skill", category="code", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Delegate Task", description="Delegate tasks to sub-agents with isolated context", kind="system_skill", category="delegation", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        # Legacy tools (kept for reference)
        Tool(name="Chart Generator", description="Generate charts and visualizations", kind="system_skill", category="visualization", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Email Sender", description="Send emails via SMTP", kind="custom_tool", category="communication", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="Database Query", description="Execute SQL queries on connected databases", kind="system_skill", category="data", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
        Tool(name="PDF Generator", description="Generate PDF documents from templates", kind="system_skill", category="file", enabled=True, status="active", source="builtin", publisher="zhanlu", created_by_id=admin.id),
    ]
    db.add_all(tools)

    # --- AgentApps ---
    # System agents (agent_builder, skill_agent, automation_agent) with tool_config
    general_tool_config = {
        "enabled_tools": [
            "web_search", "web_extract", "memory", "todo",
            "read_file", "write_file", "image_generation",
            "execute_code", "delegate_task",
            "create_dashboard", "update_dashboard", "undo_dashboard_edit",
            "uiux_search", "uiux_design_system",
        ],
    }
    agent_builder_config = {
        "enabled_tools": ["create_agent", "update_agent", "list_tools", "list_market_agents"],
    }
    skill_agent_config = {
        "enabled_tools": ["create_skill", "update_skill", "list_tools", "search_skills"],
    }
    automation_agent_config = {
        "enabled_tools": ["create_automation", "update_automation", "list_knowledge_bases"],
    }

    # Baseline Layer 3 Harness Agent fields shared by all system agents
    _base_harness = {
        "trace_enabled": True,
        "log_level": "info",
        "memory_scope": "app_shared",
        "data_bindings": [],
        "skill_bindings": [],
        "output_contract": {
            "allowed_artifact_types": ["markdown", "json", "csv", "text"],
            "must_include_sources": True,
            "citation_format": "inline",
            "max_response_length": 8192,
        },
        "evaluation_profile": {
            "test_cases": [],
            "trace_replay_enabled": True,
            "grounding_checks": ["source_citation", "hallucination_check"],
            "expected_accuracy": 0.85,
        },
    }

    agents = [
        # System meta-agents
        AgentApp(name="agent_builder", description="Builds and configures new AI agents", project="global", capabilities=["agent_creation", "configuration"], model="gpt-4o-mini", agent_type="sequential", topology="standalone", status="active", tool_config=agent_builder_config, **_base_harness, manifest_json={"agent_name":"agent_builder","version":"1.0.0","mission":"Build and configure enterprise-grade AI agents through conversational requirements gathering","task_scope":["agent_creation","agent_configuration","skill_discovery"],"boundaries":{"allowed":["create_agent","update_agent","list_tools","list_market_agents","skills","skills_hub"],"forbidden":["delete_agent","access_user_data","call_user_agent_tools"]},"risk_tier":"medium","created_by":"system"}, policy_profile={"risk_tier":"medium","requires_confirmation":True,"max_concurrent_calls":3,"rate_limit_per_minute":30,"allowed_domains":[],"retention_days":30}, created_by_id=admin.id),
        AgentApp(name="skill_agent", description="Creates and manages skills/tools", project="global", capabilities=["skill_creation", "tool_management"], model="gpt-4o-mini", agent_type="sequential", topology="standalone", status="active", tool_config=skill_agent_config, **_base_harness, manifest_json={"agent_name":"skill_agent","version":"1.0.0","mission":"Create, discover, and manage reusable skill methodology documents for agents","task_scope":["skill_creation","skill_discovery","skill_management"],"boundaries":{"allowed":["create_skill","update_skill","search_skills","list_tools"],"forbidden":["delete_skill","modify_system_skills"]},"risk_tier":"low","created_by":"system"}, policy_profile={"risk_tier":"low","requires_confirmation":False,"max_concurrent_calls":5,"rate_limit_per_minute":60,"allowed_domains":[],"retention_days":30}, created_by_id=admin.id),
        AgentApp(name="automation_agent", description="Creates automation tasks and schedules", project="global", capabilities=["automation", "scheduling"], model="gpt-4o-mini", agent_type="sequential", topology="standalone", status="active", tool_config=automation_agent_config, **_base_harness, manifest_json={"agent_name":"automation_agent","version":"1.0.0","mission":"Build scheduled automation tasks connecting data sources to actions","task_scope":["automation_creation","schedule_management","data_source_binding"],"boundaries":{"allowed":["create_automation","update_automation","list_knowledge_bases","cronjob"],"forbidden":["delete_automation","execute_automation","access_user_data"]},"risk_tier":"medium","created_by":"system"}, policy_profile={"risk_tier":"medium","requires_confirmation":True,"max_concurrent_calls":3,"rate_limit_per_minute":20,"allowed_domains":[],"retention_days":30}, created_by_id=admin.id),
        # General Assistant — all new capability tools
        AgentApp(name="general_assistant", description="A versatile AI agent with web search, memory, code execution, file operations, image generation, and task delegation capabilities", project="global", capabilities=["web_search", "memory", "todo", "code_execution", "file_operations", "image_generation", "delegation"], model="gpt-4o", agent_type="sequential", topology="standalone", status="active", tool_config=general_tool_config, **_base_harness, manifest_json={"agent_name":"general_assistant","version":"1.0.0","mission":"Serve as a versatile AI assistant with the full zhanlu toolset for general-purpose tasks","task_scope":["web_search","memory","code_execution","file_operations","delegation"],"boundaries":{"allowed":["web_search","memory","code_execution","file_operations","delegate_task"],"forbidden":["destructive_ops","impersonation","production_mutation"]},"risk_tier":"low","created_by":"system"}, policy_profile={"risk_tier":"low","requires_confirmation":False,"max_concurrent_calls":5,"rate_limit_per_minute":60,"allowed_domains":[],"retention_days":30}, created_by_id=admin.id),
        # User-facing agents
        AgentApp(name="Research Assistant", description="Helps with research tasks, summarization, and analysis", project="global", capabilities=["web_search", "summarization", "analysis"], model="gpt-4o-mini", agent_type="sequential", topology="standalone", status="active", skills=["Web Search", "Document Reader"], created_by_id=admin.id),
        AgentApp(name="Report Writer", description="Generates professional reports from data", project="global", capabilities=["report_generation", "data_analysis"], model="gpt-4o", agent_type="sequential", topology="standalone", status="active", skills=["Chart Generator", "PDF Generator"], created_by_id=admin.id),
        AgentApp(name="Data Analyst", description="Analyzes data and creates visualizations", project="Data Analysis", capabilities=["data_analysis", "visualization", "sql"], model="gpt-4o", agent_type="reactive", topology="standalone", status="draft", skills=["Database Query", "Chart Generator"], created_by_id=admin.id),
        AgentApp(name="Customer Support Agent", description="Handles customer inquiries and support tickets", project="Customer Support", capabilities=["customer_support", "ticket_routing"], model="gpt-4o-mini", agent_type="deliberative", topology="standalone", status="active", skills=["Email Sender", "Web Search"], created_by_id=admin.id),
    ]
    db.add_all(agents)

    # --- KnowledgeBases ---
    kbs = [
        KnowledgeBase(name="Product Documentation", project="global", description="Internal product documentation and user guides", type="vector_db", source_kind="file", file_type="pdf", item_count=45, status="active", created_by_id=admin.id),
        KnowledgeBase(name="Customer Database", project="Customer Support", description="Customer information and interaction history", type="business_db", source_kind="database", db_type="postgresql", host="localhost", port=5432, database_name="customers", item_count=12500, status="active", created_by_id=admin.id),
        KnowledgeBase(name="Market Research Data", project="Marketing Team", description="Market research reports and industry analysis", type="vector_db", source_kind="file", file_type="pdf", item_count=128, status="active", created_by_id=admin.id),
    ]
    db.add_all(kbs)

    # --- AutomationTasks ---
    tasks = [
        AutomationTask(name="Daily Sales Report", project="Marketing Team", type="report_generation", description="Generates a daily sales report at 9 AM", schedule="0 9 * * *", status="paused", created_by_id=admin.id),
        AutomationTask(name="Weekly Data Sync", project="Data Analysis", type="data_sync", description="Syncs data from external sources weekly", schedule="0 0 * * 0", status="paused", created_by_id=admin.id),
        AutomationTask(name="Monthly Compliance Audit", project="global", type="agent_inspection", description="Runs compliance audit agents monthly", schedule="0 0 1 * *", status="paused", created_by_id=admin.id),
        AutomationTask(name="Data Quality Check", project="Data Analysis", type="data_cleaning", description="Checks data quality and cleans invalid records", schedule="0 2 * * *", status="paused", created_by_id=admin.id),
    ]
    db.add_all(tasks)

    # --- ChatSessions + Messages ---
    session1 = ChatSession(title="Research on AI market trends", project="Marketing Team", starred=True, last_message_at="2025-07-10T10:30:00", created_by_id=admin.id)
    session2 = ChatSession(title="Customer support automation ideas", project="Customer Support", starred=False, last_message_at="2025-07-09T15:45:00", created_by_id=admin.id)
    db.add_all([session1, session2])
    db.flush()

    messages = [
        ChatMessage(session_id=session1.id, role="user", content="What are the current trends in the AI market?", order=0, created_by_id=admin.id),
        ChatMessage(session_id=session1.id, role="assistant", content="Based on recent analysis, key AI market trends include: 1) Generative AI adoption, 2) Edge AI deployment, 3) AI regulation frameworks, 4) Multimodal AI systems, 5) AI safety research.", order=1, trace=[{"step": "web_search", "query": "AI market trends 2025"}, {"step": "summarization", "source": "multiple"}], created_by_id=admin.id),
        ChatMessage(session_id=session2.id, role="user", content="How can we automate our customer support?", order=0, created_by_id=admin.id),
        ChatMessage(session_id=session2.id, role="assistant", content="You can automate customer support by: 1) Implementing an AI chatbot for first-line responses, 2) Auto-routing tickets based on content analysis, 3) Generating suggested responses for agents, 4) Creating automated FAQs from historical tickets.", order=1, created_by_id=admin.id),
    ]
    db.add_all(messages)

    # --- UserSetting ---
    user_setting = UserSetting(
        language="zh", timezone="Asia/Shanghai", date_format="YYYY-MM-DD",
        default_model="gpt-4o-mini", fallback_model="gpt-4o",
        max_tokens=4096, temperature=0.7,
        file_upload_enabled=True, session_retention_days=30,
        monitoring_enabled=True, log_level="info",
        audit_logging=True, compliance_mode="standard",
        encryption_enabled=True, usage_analytics=True,
        created_by_id=admin.id,
    )
    db.add(user_setting)

    # --- Reports ---
    reports = [
        Report(title="Q2 2025 Market Analysis", type="market_analysis", summary="Comprehensive analysis of Q2 2025 market trends", status="ready", file_url="", created_by_id=admin.id),
        Report(title="Customer Satisfaction Report", type="customer_report", summary="Monthly customer satisfaction metrics", status="ready", file_url="", created_by_id=admin.id),
    ]
    db.add_all(reports)

    # --- DecisionFlows ---
    flows = [
        DecisionFlow(name="Customer Escalation Flow", description="Decision flow for escalating customer issues", steps=5, status="active", created_by_id=admin.id),
        DecisionFlow(name="Data Validation Flow", description="Validates incoming data against schema rules", steps=3, status="draft", created_by_id=admin.id),
    ]
    db.add_all(flows)

    # --- McpServers ---
    mcps = [
        McpServer(name="Local Filesystem MCP", description="Access local filesystem resources", server_url="stdio://localhost", transport="stdio", status="disconnected", tools_count=0, resources_count=0, created_by_id=admin.id),
    ]
    db.add_all(mcps)

    db.commit()

    # ── Enterprise BI forecast targets ──────────────────────────
    # Ensure the 12 dashboard product targets exist so the nightly
    # forecast scheduler has something to compute against.
    from app.services.forecasting.seed_targets import seed_forecast_targets
    new_targets = seed_forecast_targets(db)
    if new_targets:
        print(f"  Seeded {new_targets} new enterprise BI forecast targets")

    db.close()
    print("Seed complete!")
    print("Admin user: admin@zhanlu.dev / admin123")


if __name__ == "__main__":
    seed()
