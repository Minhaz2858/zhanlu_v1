# Zhanlu Final Coding Agent Resources v3.8

This package contains the final resources to give to Claude Code or another coding agent.

## Read order

1. `00_FINAL_READ_ME_FOR_CODING_AGENT.md`
2. `00_READ_THIS_BACKEND_DECISION.md`
3. `00_read_first/CLAUDE_CODE_MASTER_BUILD_PROMPT.md`
4. `00_read_first/Zhanlu_Existing_UI_Integration_Guide.md`
5. `09_final_ui_decisions/Zhanlu_Capabilities_Instead_of_Raw_Skills_UI_FINAL.md`
6. `08_latest_ui_runtime_datasource_specs/Zhanlu_Main_Agent_Subagent_UI_ADK_Inspired_Architecture_FINAL.md`
7. `08_latest_ui_runtime_datasource_specs/Zhanlu_Central_Datasource_Connector_and_Agent_Data_Binding_FINAL.md`
8. `01_api_contract/Zhanlu_API_Contract_For_Existing_UI.md`
9. `02_database/Zhanlu_Database_Schema_v1.md`
10. `03_runtime_contracts/Zhanlu_Event_Stream_Contract.md`
11. `04_sandbox_artifacts/Zhanlu_Sandbox_Runtime_Implementation_Spec.md`
12. `04_sandbox_artifacts/Zhanlu_Artifact_Preview_Implementation_Spec.md`
13. `08_latest_ui_runtime_datasource_specs/Zhanlu_Docker_Sandbox_Setup_Runbook_FINAL.md`
14. `06_mvp_testing/Zhanlu_MVP_Build_Scope.md`
15. `06_mvp_testing/Zhanlu_Testing_Checklist.md`

## Most important final UI decision

Normal users should not select raw internal skills. They should select friendly Core Capabilities such as Make PPT, Database Analysis, Make Dashboard, Make DOCX, Generate HTML, and Scheduled Reports. Zhanlu maps these capabilities to skills, subagents, sandbox permissions, artifact types, and slash commands. Raw skills remain available only in Advanced Settings for developers/admins.

## Final backend decision

The backend can start now using the user's existing UI. Do not rebuild UI from zero. Build a FastAPI/PostgreSQL/Redis/Docker sandbox backend that supports main agents, subagents, capabilities, per-agent datasource binding, skills, slash commands, artifacts, inline previews, live execution timeline, and sandbox execution.
