# Jira Triage Feature

## Objective
Develop the Jira Triage feature.

## Description
This feature is used to analyze Jira tickets and classify them based on severity and priority.

## Features
- Analyze Jira tickets
- Classify severity (S1–S4)
- Assign priority
- Route tickets to the appropriate team

## Status
In Progress

## Author
Shravani

# Jira Ticket Triage using Retrieval-Augmented Generation (RAG)

## Overview

This project uses **Retrieval-Augmented Generation (RAG)** to improve Jira ticket triage. Instead of relying only on a Large Language Model (LLM), the system retrieves relevant historical Jira tickets and operational runbooks from a vector database before generating recommendations.

By combining semantic search with LLM reasoning, the system provides more accurate severity classification, priority recommendations, and suggested resolutions.

---

## Why RAG?

Traditional LLMs generate responses based only on their trained knowledge, which may not include an organization's internal incidents or runbooks.

RAG solves this problem by retrieving relevant organizational knowledge before the LLM generates a response.

### Benefits

* Improves severity prediction accuracy.
* Reduces hallucinations.
* Reuses historical incident knowledge.
* Provides consistent recommendations.
* Supports human-in-the-loop review for critical incidents.

---

## RAG Workflow

```text
User submits Jira Ticket
            │
            ▼
Generate Embedding
            │
            ▼
pgvector Similarity Search
            │
            ▼
Retrieve Historical Tickets
            │
            ▼
Retrieve Runbooks
            │
            ▼
Provide Context to LLM
            │
            ▼
Severity, Priority & Resolution Suggestion
```

---

## Project Components

### Historical Tickets

Historical Jira incidents are stored with vector embeddings.

Each ticket contains information such as:

* Summary
* Description
* Component
* Severity
* Resolution

These records help identify incidents similar to the current ticket.

---

### Runbooks

Operational runbooks provide troubleshooting steps and standard operating procedures.

Examples include:

* Database recovery
* API restart procedures
* Service health checks
* Incident response guidelines

The retriever returns the most relevant runbooks based on semantic similarity.

---

### Embedding Generation

The `embed_text()` function converts the user query into a numerical vector representation.

This allows semantic comparison instead of simple keyword matching.

Example query:

```
Production API is unavailable for all users.
```

The query is converted into an embedding and compared with stored ticket embeddings.

---

### Vector Search

The project uses **pgvector** with PostgreSQL.

Similarity search is performed using cosine distance.

The retriever returns the top matching historical tickets and runbooks.

---

## Retrieval Functions

### search_similar_tickets()

Purpose:

* Retrieves semantically similar historical Jira tickets.
* Returns previous resolutions for similar incidents.

Returned fields include:

* ID
* Summary
* Description
* Component
* Severity
* Resolution

---

### search_runbooks()

Purpose:

* Retrieves operational documentation.
* Supports optional filtering by document type.
* Returns the most relevant troubleshooting guides.

Returned fields include:

* ID
* Document Type
* Title
* Content

---

## Example

### User Query

```
The production API is returning HTTP 500 errors.
```

### Retrieved Historical Ticket

* Production API outage
* Severity: S1
* Resolution: Restart backend service and verify database connectivity.

### Retrieved Runbook

**Title:** API Recovery Guide

Steps:

1. Check service health.
2. Verify database connection.
3. Restart affected service.
4. Validate application logs.

### LLM Recommendation

Severity: **S1**

Priority: **Highest**

Reason:

The issue affects production users and closely matches previous critical incidents stored in the knowledge base.

Suggested Action:

Follow the API Recovery Runbook and monitor service health after deployment.

---

## Advantages of This Approach

* Faster incident triage.
* Better decision consistency.
* Knowledge reuse from previous incidents.
* Improved recommendation quality.
* Easy integration with Jira workflows.
* Scalable semantic search using pgvector.

---

## Conclusion

The RAG pipeline combines semantic retrieval with LLM reasoning to improve Jira ticket triage. By retrieving similar historical incidents and operational runbooks before generating recommendations, the system produces more reliable severity classifications and actionable resolution guidance while leveraging organizational knowledge effectively.
