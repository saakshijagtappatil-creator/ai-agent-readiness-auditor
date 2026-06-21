---
name: llms-txt-drafting
description: Drafts a high-quality llms.txt file describing a website's purpose, routes, and key functionality to pass Lighthouse agentic browsing validation.
---
# llms-txt-drafting Skill

Use this skill to draft a high-quality `llms.txt` file content when the site lacks one (or when check_id "llms-txt-exists" or "llms-txt" failed, or when the diagnosis asks for "llms_txt" remediation).

## Critical Validation Requirements
To pass validation, the drafted `llms.txt` content MUST strictly satisfy the following format requirements:
1. **H1 Header**: The file must start with exactly one H1 header (`# <Title>`) representing the site or repository name.
2. **Markdown Links**: The file must contain at least one valid Markdown link (e.g., `[Link Text](url)` or `[Link Text](relative_path)`).
   - **CRITICAL fallback**: If no specific URLs or links are found in the target context/diagnosis/HTML contents, you MUST create a link pointing to the root page or index file (e.g., `[Home Page](/)` or `[Main](index.html)`). Do not write "Home Page" as plain text.

## Content Requirements
The drafted content must:
- Describe the primary purpose of the site or repository.
- Detail the key routes, endpoints, or modules and their functionalities.
- Be structured cleanly and concisely for LLM parsing.

## Example of a Valid llms.txt Draft
```markdown
# My Website Title

This website provides simple tools for managing tasks and user profiles.

## Main Navigation
- [Home Page](/)
```
