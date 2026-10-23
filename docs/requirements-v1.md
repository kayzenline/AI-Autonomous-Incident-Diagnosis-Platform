# AgentOps — AI Autonomous Incident Diagnosis Platform

**Document Type:** Product Requirements Document / Software Requirements Specification  
**Project Type:** AI Application Engineering / Agent System  
**Target Users:** Developers, DevOps engineers, SRE engineers  
**Primary Goal:** Build an AI-powered system that automatically investigates software/system incidents by collecting evidence from multiple tools, reasoning over the evidence, identifying likely root causes, and producing structured remediation recommendations.

---

# 1. Project Overview

## 1.1 Project Name

**AgentOps — AI Autonomous Incident Diagnosis Platform**

## 1.2 Project Summary

AgentOps is an AI-powered incident diagnosis system designed to assist developers and infrastructure engineers when software services experience operational failures.

Typical incidents include:

- API latency suddenly increasing
- HTTP 500 / 502 / 503 errors
- CPU usage becoming abnormally high
- Memory exhaustion
- Database connection failures
- Service crashes
- Disk space exhaustion
- Network connectivity issues
- DNS resolution failures
- Authentication failures

Instead of requiring an engineer to manually inspect metrics, logs, processes, databases, and network status one by one, AgentOps will automatically collect relevant system information through predefined tools.

The AI Agent will then:

1. Understand the incident reported by the user.
2. Determine what evidence is required.
3. Select and call appropriate diagnostic tools.
4. Analyse the returned evidence.
5. Continue investigation if necessary.
6. Determine the most likely root cause.
7. Assign severity and confidence.
8. Recommend remediation steps.
9. Store the investigation process.
10. Evaluate whether the diagnosis was correct.

---

# 2. Problem Statement

When a software system fails, developers often need to manually investigate several sources:

```text
Metrics
Logs
Processes
Database
Network
Configuration
Application status
```

For example:

```text
User reports:

"Our API suddenly became very slow."
```

A human engineer may need to check:

```text
CPU usage
↓
Memory usage
↓
Running processes
↓
Application logs
↓
Database response time
↓
Network latency
↓
Recent errors
```

This process can be repetitive and requires experience.

AgentOps attempts to automate the initial investigation process using an AI Agent.

---

# 3. Product Vision

The long-term vision is:

```text
Incident
   ↓
AI Agent
   ↓
Automatically collect evidence
   ↓
Reason about system state
   ↓
Identify root cause
   ↓
Recommend remediation
   ↓
Evaluate diagnosis quality
```

The system should behave more like a junior SRE engineer than a chatbot.

AgentOps should not simply answer questions based on the user's description.

It should actively investigate.

---

# 4. Project Objectives

The project has two categories of objectives.

## 4.1 Product Objectives

The final product should be able to:

- Accept incident reports through REST APIs.
- Represent incidents using structured data.
- Query simulated infrastructure diagnostic tools.
- Use an LLM to interpret incidents.
- Use LLM Function Calling to invoke tools.
- Perform multi-step investigation.
- Store previous incidents and diagnostic runs.
- Retrieve similar historical incidents using RAG.
- Use multiple reasoning stages or agents.
- Support tools through MCP.
- Evaluate diagnosis quality.
- Run inside Docker containers.
- Be automatically tested using CI/CD.

---

## 4.2 Learning Objectives

The project should provide practical experience with:

### Programming

- Python
- Python typing
- Object-Oriented Programming
- asynchronous programming
- exception handling
- testing

### Backend Engineering

- FastAPI
- REST APIs
- HTTP
- Pydantic
- backend architecture
- dependency management

### Database

- SQL
- PostgreSQL
- relational modelling
- SQLAlchemy
- pgvector

### AI Engineering

- LLM APIs
- prompt engineering
- structured output
- tool calling
- agent loops
- context management
- RAG
- embeddings
- reranking
- agent workflows
- LLM evaluation

### Infrastructure

- Linux
- Docker
- Docker Compose
- environment variables
- logging
- CI/CD
- GitHub Actions

### Agent Technologies

- LangGraph
- MCP
- multi-agent workflows

---

# 5. Target Users

## 5.1 Primary User

**Software Engineer / DevOps Engineer / SRE**

The user has detected an issue but does not yet know the root cause.

Example:

```text
"Our payment API is returning 502 errors."
```

The engineer wants AgentOps to perform an initial investigation.

---

## 5.2 Secondary User

**Engineering Manager**

The manager may want to:

- inspect previous incidents
- review diagnosis accuracy
- analyse common failures
- review Agent performance

---

# 6. Core User Stories

## US-01 — Create Incident

As an engineer, I want to submit an incident so that AgentOps can begin diagnosis.

Example:

```text
POST /incidents
```

Input:

```json
{
  "title": "Payment API returning 502",
  "description": "Users started receiving 502 errors approximately 10 minutes ago."
}
```

---

## US-02 — Run Diagnosis

As an engineer, I want AgentOps to analyse an incident so that I can identify its likely root cause.

Example:

```text
POST /incidents/{incident_id}/diagnose
```

---

## US-03 — Automatically Use Tools

As an engineer, I want AgentOps to automatically select diagnostic tools rather than requiring me to manually inspect the system.

Possible tools:

```text
get_cpu_usage()
get_memory_usage()
get_disk_usage()
get_process_list()
get_application_logs()
get_database_status()
get_network_status()
get_service_status()
```

---

## US-04 — View Investigation Trace

As an engineer, I want to see which tools the Agent used and what evidence it collected.

Example:

```text
Step 1
Tool: get_cpu_usage
Result: 97%

Step 2
Tool: get_process_list
Result: python-api = 89%

Step 3
Tool: get_application_logs
Result: infinite processing loop detected
```

---

## US-05 — Receive Root Cause

The system should return:

```json
{
  "root_cause": "Application worker entered a CPU-intensive infinite loop",
  "severity": "critical",
  "confidence": 0.91
}
```

---

## US-06 — Receive Recommendations

The system should provide actionable recommendations.

Example:

```json
{
  "recommendations": [
    "Restart the affected worker process",
    "Inspect the request processing loop",
    "Add timeout protection",
    "Add CPU usage alerting"
  ]
}
```

---

## US-07 — Search Historical Incidents

As an engineer, I want AgentOps to retrieve similar historical incidents to help identify recurring problems.

---

## US-08 — Evaluate Agent Performance

As a developer of AgentOps, I want to run benchmark incidents and measure diagnostic performance.

Metrics may include:

```text
Diagnosis Accuracy
Tool Selection Accuracy
Task Success Rate
Average Latency
Average Token Usage
Average Cost
```

---

# 7. Functional Requirements

---

# FR-01 Incident Management

The system must allow users to:

- create incidents
- retrieve incidents
- list incidents
- update incident status
- delete incidents

Each incident must contain:

```text
incident_id
title
description
status
severity
created_at
updated_at
```

Possible statuses:

```text
OPEN
INVESTIGATING
DIAGNOSED
RESOLVED
FAILED
```

---

# FR-02 Structured Incident Input

The system must validate incoming incident data using Pydantic.

Example model:

```text
IncidentCreate

title
description
optional metadata
```

Invalid input must return appropriate HTTP errors.

Example:

```text
400 Bad Request
422 Unprocessable Entity
```

---

# FR-03 Diagnostic Run

Each diagnosis must create a separate diagnostic run.

A run contains:

```text
run_id
incident_id
status
started_at
completed_at
root_cause
severity
confidence
```

Possible statuses:

```text
PENDING
RUNNING
COMPLETED
FAILED
```

---

# FR-04 Diagnostic Tools

The system must support tools that provide simulated infrastructure information.

Minimum tool set:

### System Tools

```text
get_cpu_usage
get_memory_usage
get_disk_usage
get_process_list
```

### Application Tools

```text
get_application_logs
get_service_status
```

### Database Tools

```text
get_database_status
get_database_latency
```

### Network Tools

```text
get_network_status
get_dns_status
```

Each tool must:

1. Accept structured input.
2. Return structured output.
3. Handle errors.
4. Produce logs.

---

# FR-05 LLM Integration

The system must integrate with at least one LLM provider.

The LLM layer must support:

- system prompts
- user messages
- structured output
- tool definitions
- tool calls
- tool result messages

The LLM provider must be abstracted so that the implementation can later support multiple providers.

Conceptually:

```text
LLMProvider

call()
call_with_tools()
structured_output()
```

---

# FR-06 Structured Diagnosis

The AI must return structured diagnosis information.

Required fields:

```text
root_cause
severity
confidence
evidence
recommendations
```

Example:

```json
{
  "root_cause": "Database connection pool exhaustion",
  "severity": "high",
  "confidence": 0.87,
  "evidence": [
    "Database active connections reached maximum",
    "Application logs show connection timeout",
    "CPU usage is normal"
  ],
  "recommendations": [
    "Increase connection pool capacity",
    "Investigate unreleased connections",
    "Add connection pool monitoring"
  ]
}
```

---

# FR-07 Tool Calling

The LLM must be able to decide when a tool should be executed.

Example:

```text
User:
Why is my API slow?

Agent:
I need CPU information.

Tool Call:
get_cpu_usage()

Tool Result:
96%

Agent:
I need to identify the responsible process.

Tool Call:
get_process_list()
```

Tool execution must happen in application code.

The LLM must never directly execute shell commands.

---

# FR-08 Agent Loop

The application must eventually implement its own Agent loop before introducing an Agent framework.

Conceptual behaviour:

```text
while diagnosis_not_complete:

    send context to LLM

    if LLM requests tool:
        execute tool
        append result

    else:
        return final diagnosis
```

The loop must include:

```text
maximum iteration count
tool execution timeout
invalid tool handling
LLM failure handling
```

---

# FR-09 Investigation Trace

Every diagnosis must store a trace.

A trace step contains:

```text
step_id
run_id
step_number
type
tool_name
tool_input
tool_output
timestamp
```

Possible step types:

```text
LLM_REASONING
TOOL_CALL
TOOL_RESULT
FINAL_DIAGNOSIS
ERROR
```

Private model chain-of-thought must not be stored.

Only externally observable reasoning summaries or decisions should be stored.

---

# FR-10 Persistence

Data must eventually be stored in PostgreSQL.

Required entities:

```text
Incident
DiagnosticRun
DiagnosticStep
ToolExecution
HistoricalIncident
EvaluationResult
```

---

# FR-11 Historical Incident Retrieval

The system must support retrieval of historical incident reports.

Each historical incident should contain:

```text
symptoms
root_cause
solution
embedding
metadata
```

The system should retrieve similar incidents using vector similarity.

---

# FR-12 RAG

The system must eventually support Retrieval-Augmented Generation.

Pipeline:

```text
Historical Incident
↓
Text Processing
↓
Chunking
↓
Embedding
↓
pgvector
↓
Similarity Search
↓
Top-K Results
↓
LLM Context
```

The system should support:

```text
Top-K retrieval
similarity threshold
metadata filtering
```

Optional future extension:

```text
reranking
```

---

# FR-13 Agent Workflow

After the manual Agent loop is completed, the system may introduce LangGraph.

Initial workflow:

```text
START
  ↓
Analyse Incident
  ↓
Select Tool
  ↓
Execute Tool
  ↓
Evaluate Evidence
  ↓
Enough Evidence?
  ├── No → Select Tool
  └── Yes
         ↓
    Final Diagnosis
         ↓
        END
```

---

# FR-14 Multi-Agent Architecture

Later versions may split responsibilities.

Possible agents:

### Planner Agent

Determines investigation strategy.

### System Agent

Investigates:

```text
CPU
Memory
Disk
Processes
```

### Database Agent

Investigates database health.

### Network Agent

Investigates network problems.

### Log Analysis Agent

Analyses logs.

### Root Cause Agent

Combines evidence.

### Verification Agent

Challenges the proposed diagnosis.

Conceptual architecture:

```text
                   ┌─ System Agent
                   │
Incident → Planner ├─ Database Agent
                   │
                   ├─ Network Agent
                   │
                   └─ Log Agent
                          ↓
                  Root Cause Agent
                          ↓
                  Verification Agent
```

Multi-agent architecture is NOT required for early versions.

---

# FR-15 MCP Support

The system should later expose or consume diagnostic tools through Model Context Protocol.

Example MCP tools:

```text
get_cpu_usage
get_logs
get_database_status
search_incidents
```

AgentOps should include at least one custom MCP server.

---

# FR-16 Evaluation Framework

The system must support benchmark cases.

Each benchmark contains:

```text
incident
environment state
expected root cause
expected relevant tools
expected severity
```

Example:

```json
{
  "incident": "API latency increased",
  "expected_root_cause": "CPU overload",
  "expected_tools": [
    "get_cpu_usage",
    "get_process_list"
  ]
}
```

---

# FR-17 Evaluation Metrics

The system should calculate:

## Diagnosis Accuracy

```text
correct diagnoses / total incidents
```

## Tool Selection Accuracy

Whether the Agent selected appropriate tools.

## Task Success Rate

Whether the incident was correctly resolved.

## Latency

```text
diagnosis completion time
```

## Token Consumption

```text
input tokens
output tokens
total tokens
```

## Estimated Cost

Approximate model API cost.

---

# FR-18 Logging

Application logs must record:

```text
API requests
diagnostic runs
tool calls
tool failures
database failures
LLM failures
execution latency
```

Logs should use Python's standard logging framework.

Passwords, API keys and secrets must never appear in logs.

---

# FR-19 Error Handling

The system must handle:

```text
Invalid incident
Unknown tool
Tool failure
Tool timeout
LLM API failure
LLM timeout
Malformed LLM output
Database failure
Maximum Agent iterations exceeded
```

Errors should be converted into appropriate application-level exceptions.

---

# FR-20 API Documentation

FastAPI must automatically expose:

```text
/docs
```

Swagger/OpenAPI documentation should describe all public endpoints.

---

# 8. API Requirements

The final API may include the following endpoints.

## Health

```text
GET /health
```

---

## Incidents

```text
POST   /incidents
GET    /incidents
GET    /incidents/{incident_id}
PATCH  /incidents/{incident_id}
DELETE /incidents/{incident_id}
```

---

## Diagnosis

```text
POST /incidents/{incident_id}/diagnose
```

---

## Runs

```text
GET /runs/{run_id}
GET /runs/{run_id}/steps
```

---

## Historical Search

```text
GET /incidents/search
```

Example:

```text
GET /incidents/search?q=database timeout
```

---

## Evaluation

```text
POST /evaluations/run
GET  /evaluations/{evaluation_id}
```

---

# 9. Initial Data Model

## Incident

```text
id
title
description
status
severity
created_at
updated_at
```

Relationship:

```text
Incident
   |
   | 1:N
   ↓
DiagnosticRun
```

---

## DiagnosticRun

```text
id
incident_id
status
root_cause
severity
confidence
started_at
completed_at
```

Relationship:

```text
DiagnosticRun
   |
   | 1:N
   ↓
DiagnosticStep
```

---

## DiagnosticStep

```text
id
run_id
step_number
step_type
tool_name
tool_input
tool_output
created_at
```

---

# 10. Non-Functional Requirements

## NFR-01 Maintainability

Code must follow modular architecture.

Suggested structure:

```text
app/
│
├── api/
├── models/
├── schemas/
├── services/
├── agents/
├── tools/
├── repositories/
├── llm/
├── evaluation/
└── core/
```

---

## NFR-02 Testability

Core functionality must be unit-testable.

External services such as LLM APIs must be mockable.

---

## NFR-03 Reliability

One tool failure should not necessarily crash the entire diagnostic process.

---

## NFR-04 Security

Secrets must be stored using environment variables.

Example:

```text
LLM_API_KEY
DATABASE_URL
```

`.env` must not be committed to Git.

---

## NFR-05 Performance

Early versions should aim for:

```text
Normal API request:
< 500 ms excluding LLM calls

Typical AI diagnosis:
< 30 seconds
```

Exact performance targets may change during development.

---

## NFR-06 Observability

Important operations should generate logs.

Future versions may additionally expose:

```text
metrics
traces
```

---

## NFR-07 Extensibility

The architecture should allow new tools to be added without rewriting the Agent.

For example:

```text
get_kubernetes_status
get_redis_status
get_container_logs
```

---

# 11. System Architecture

Target architecture:

```text
                    ┌──────────────────┐
                    │      Client      │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │     FastAPI      │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Incident Service │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │  Agent Runtime   │
                    └────────┬─────────┘
                             │
                ┌────────────┴─────────────┐
                │                          │
                ▼                          ▼
        ┌───────────────┐          ┌───────────────┐
        │      LLM      │          │     Tools     │
        └───────────────┘          └───────┬───────┘
                                           │
                     ┌─────────────────────┼────────────────────┐
                     ▼                     ▼                    ▼
                  Metrics                Logs                Database
                     │
                     ▼
                System State

                             │
                             ▼
                    ┌──────────────────┐
                    │   PostgreSQL     │
                    │   + pgvector     │
                    └──────────────────┘
```

---

# 12. Development Strategy

The project must be built incrementally.

Each version must remain runnable.

A later technology must only be introduced when its purpose can be explained.

---

# 13. Version Roadmap

# Version 0 — Rule-Based Backend

## Goal

Build a functioning backend without AI.

Architecture:

```text
User
 ↓
FastAPI
 ↓
Python Logic
 ↓
Diagnosis
```

Requirements:

- FastAPI application
- incident request model
- diagnosis endpoint
- simple rule-based diagnosis
- validation
- unit tests

Example:

```text
CPU > 90%

→ HIGH_CPU_USAGE
```

Skills:

```text
Python
FastAPI
Pydantic
HTTP
pytest
```

---

# Version 1 — LLM Diagnosis

Add:

```text
LLM API
```

Requirements:

- LLM provider abstraction
- prompts
- structured output
- environment variables
- error handling

Skills:

```text
LLM API
Prompt Engineering
Structured Output
```

---

# Version 2 — Tool Calling

Add:

```text
Function Calling
```

Tools:

```text
get_cpu_usage
get_memory_usage
get_logs
get_process_list
```

Skills:

```text
Tool Calling
JSON Schema
Agent fundamentals
```

---

# Version 3 — Persistence

Add:

```text
PostgreSQL
SQLAlchemy
```

Persist:

```text
Incidents
Runs
Steps
Tool calls
```

Skills:

```text
SQL
PostgreSQL
ORM
Database design
```

---

# Version 4 — RAG

Add:

```text
Historical Incident Retrieval
```

Technology:

```text
Embeddings
pgvector
Top-K retrieval
```

Skills:

```text
RAG
Vector search
Embeddings
```

---

# Version 5 — Manual Agent Runtime

Implement a custom Agent loop.

Features:

```text
multiple tool calls
maximum iterations
dynamic investigation
```

Skills:

```text
Agent architecture
Context management
State management
```

---

# Version 6 — LangGraph

Refactor Agent runtime using LangGraph.

Features:

```text
workflow
branching
state
retry
```

Skills:

```text
Agent orchestration
Graph-based workflows
```

---

# Version 7 — MCP

Create custom MCP server.

Expose tools:

```text
get_logs
get_cpu_usage
search_incidents
```

Skills:

```text
MCP
Tool protocol design
```

---

# Version 8 — Evaluation

Create benchmark dataset.

Minimum benchmark size:

```text
20 incidents
```

Target later:

```text
50–100 incidents
```

Track:

```text
Accuracy
Tool selection
Latency
Cost
Token usage
```

Skills:

```text
LLM Evaluation
Benchmarking
Metrics
```

---

# Version 9 — Docker

Containerise:

```text
FastAPI
PostgreSQL
```

Use:

```text
Docker
Docker Compose
```

Skills:

```text
Containerisation
Deployment
Infrastructure
```

---

# Version 10 — CI/CD

GitHub Actions should run:

```text
lint
unit tests
integration tests
```

on each:

```text
push
pull request
```

Skills:

```text
CI/CD
GitHub Actions
Production engineering
```

---

# 14. Initial Incident Scenarios

The project should eventually support at least the following incident categories.

## Scenario 1 — High CPU

Evidence:

```text
CPU = 97%
python-api = 89%
```

Expected root cause:

```text
CPU intensive application process
```

---

## Scenario 2 — Memory Leak

Evidence:

```text
memory = 96%
application memory increasing continuously
```

Root cause:

```text
memory leak
```

---

## Scenario 3 — Database Connection Pool Exhaustion

Evidence:

```text
active connections = maximum
logs = timeout acquiring connection
```

Root cause:

```text
database connection pool exhausted
```

---

## Scenario 4 — Disk Full

Evidence:

```text
disk = 99.8%
```

Root cause:

```text
insufficient disk space
```

---

## Scenario 5 — DNS Failure

Evidence:

```text
DNS lookup failure
service otherwise healthy
```

Root cause:

```text
DNS resolution failure
```

---

## Scenario 6 — Service Crash

Evidence:

```text
service status = DOWN
logs = uncaught exception
```

Root cause:

```text
application crash
```

---

## Scenario 7 — Slow Database

Evidence:

```text
CPU normal
application healthy
database latency = 3000 ms
```

Root cause:

```text
database performance degradation
```

---

## Scenario 8 — Authentication Failure

Evidence:

```text
HTTP 401 rate increased
authentication logs show expired credentials
```

Root cause:

```text
invalid or expired authentication configuration
```

---

## Scenario 9 — Network Failure

Evidence:

```text
service running
database healthy
packet loss high
```

Root cause:

```text
network connectivity issue
```

---

## Scenario 10 — HTTP 502

Evidence:

```text
reverse proxy healthy
upstream service unavailable
```

Root cause:

```text
upstream application failure
```

---

# 15. Testing Requirements

The project should contain:

## Unit Tests

Test:

```text
tool functions
Pydantic validation
services
diagnosis rules
```

---

## Integration Tests

Test:

```text
API → Service
Service → Database
Agent → Tools
```

---

## Agent Evaluation

Test AI behaviour separately from ordinary unit tests.

This distinction is important:

```text
Software Test
≠
AI Evaluation
```

---

# 16. Git Requirements

Development should use Git from the beginning.

Suggested branches:

```text
main
feature/*
fix/*
```

Examples:

```text
feature/incident-api
feature/tool-calling
feature/postgresql
feature/rag
```

Commit messages should be meaningful.

Examples:

```text
feat: add incident creation endpoint
feat: implement CPU diagnostic tool
test: add incident validation tests
fix: handle malformed LLM output
```

---

# 17. Documentation Requirements

Repository should eventually contain:

```text
README.md
docs/
    architecture.md
    agent-design.md
    evaluation.md
```

README should include:

- project overview
- architecture diagram
- features
- technology stack
- installation
- usage
- example diagnosis
- benchmark results

---

# 18. Definition of Done

The complete project is considered successful when:

1. A user can submit an incident through an API.
2. An AI Agent can independently decide which tools to use.
3. Multiple tools can be used during one investigation.
4. Evidence from tools is used to determine root cause.
5. Diagnosis is returned using structured output.
6. Investigation history is persisted.
7. Historical incidents can be retrieved using RAG.
8. At least one MCP server is implemented.
9. Agent behaviour can be benchmarked.
10. The system runs through Docker Compose.
11. Automated tests run through GitHub Actions.
12. README clearly demonstrates architecture and results.

---

# 19. Target Final Demo

A final demonstration should look like this.

User request:

```text
Our payment API suddenly became extremely slow.
Investigate the issue.
```

Agent:

```text
Investigation started.
```

Tool:

```text
get_cpu_usage()
```

Result:

```text
CPU: 94%
```

Agent:

```text
CPU usage is abnormal. I will inspect running processes.
```

Tool:

```text
get_process_list()
```

Result:

```text
payment-api: 82%
postgres: 4%
nginx: 2%
```

Agent:

```text
The payment service is consuming most CPU.
I will inspect recent application logs.
```

Tool:

```text
get_application_logs()
```

Result:

```text
Repeated execution detected in order validation loop.
```

Agent final response:

```json
{
  "root_cause": "Infinite loop in payment order validation logic",
  "severity": "critical",
  "confidence": 0.94,
  "evidence": [
    "CPU usage reached 94%",
    "payment-api consumed 82% CPU",
    "logs show repeated order validation execution"
  ],
  "recommendations": [
    "Restart the affected payment API instance",
    "Inspect the order validation loop",
    "Introduce execution timeout protection",
    "Add CPU utilisation alerting"
  ]
}
```

---

# 20. Engineering Principles

During development, the following rules must be followed.

## Rule 1

Do not introduce technology simply because it looks impressive on a résumé.

Every technology must solve an identifiable problem.

---

## Rule 2

Build the simplest working implementation first.

Example:

```text
manual Agent loop
before
LangGraph
```

---

## Rule 3

Every major component must be explainable.

The developer should understand:

```text
what it does
why it exists
why this implementation was chosen
what alternative approaches exist
```

---

## Rule 4

AI must not hide poor software engineering.

Standard engineering practices still apply:

```text
typing
testing
logging
error handling
modular design
version control
```

---

## Rule 5

The project should demonstrate progression.

The final Git history should clearly show evolution from:

```text
Simple Python backend
↓
LLM application
↓
Tool-using Agent
↓
Persistent Agent system
↓
RAG
↓
Agent workflow
↓
Evaluation
↓
Production-style deployment
```

This evolution is part of the project's value.

---

# 21. Intended Résumé Positioning

When completed, the project should support résumé points similar to:

**AgentOps — Autonomous AI Incident Diagnosis Platform**

- Designed and built an AI-powered incident diagnosis platform using Python, FastAPI and PostgreSQL, enabling autonomous investigation of system failures through structured tool execution.
- Implemented a tool-using LLM Agent capable of dynamically inspecting system metrics, application logs, databases and network state to determine probable root causes.
- Built Retrieval-Augmented Generation over historical incidents using PostgreSQL and pgvector to incorporate previous troubleshooting knowledge.
- Developed Agent workflows with LangGraph and exposed diagnostic capabilities through MCP.
- Created an evaluation framework measuring diagnosis accuracy, tool-selection accuracy, latency and model cost across benchmark incident scenarios.
- Containerised the application with Docker and implemented automated testing through GitHub Actions.

These résumé points should only be used after the corresponding functionality has actually been implemented.

---

# 22. Final Project Technology Stack

Target stack:

```text
Language
├── Python

Backend
├── FastAPI
├── Pydantic
└── SQLAlchemy

Database
├── PostgreSQL
└── pgvector

AI
├── LLM API
├── Structured Output
├── Function Calling
├── RAG
├── LangGraph
└── MCP

Infrastructure
├── Linux
├── Docker
├── Docker Compose
└── GitHub Actions

Testing
├── pytest
└── Agent Evaluation Framework
```

---

# 23. First Milestone

Development begins with **Version 0**.

The first milestone intentionally contains:

```text
NO LLM
NO Agent
NO RAG
NO LangGraph
NO MCP
NO Docker
```

The goal is only to build a clean Python/FastAPI backend that accepts an incident and produces a deterministic diagnosis.

Version 0 is complete only when the developer can independently explain:

- how a Python project is structured
- how FastAPI starts
- what an HTTP endpoint is
- POST vs GET
- request body
- JSON
- Pydantic model
- type hints
- HTTP status codes
- exception handling
- basic pytest tests

Only after Version 0 is understood and working should Version 1 begin.