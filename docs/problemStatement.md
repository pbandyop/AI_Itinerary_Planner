
Applied Generative AI Bootcamp
DetailsDetails
SubmissionsSubmissions
Submission pending
Graduation Project - Jul 26 - Jun 2026

Submission deadline :
Jul 25, 11:29:00 AM (America/Los_Angeles)
Objective
Build a voice-first AI travel planning assistant that can understand spoken trip requests, generate a realistic and grounded itinerary, allow edits via voice, and explain its decisions clearly.

This capstone is designed to test whether you can design, build, and evaluate a real GenAI system, not just a prompt-based demo.

Problem Statement
People don’t struggle to find places to visit.They struggle to turn preferences, time constraints, travel effort, weather, and personal pace into a doable plan.

Your task is to build a voice-based AI assistant that:

Collects trip preferences conversationally
Creates a day-wise itinerary that is feasible
Allows the user to modify the plan using voice commands
Explains why each decision was made
Grounds recommendations in publicly available data
Along with the above, implement a n8n workflow that will generate a PDF itinerary and email it to the user.

What You Will Build
A deployed voice-mode travel planner with a minimal companion UI.

Core Capabilities (Required)
1. Voice-Based Trip Planning
The assistant must support spoken inputs like:

“Plan a 3-day trip to Jaipur next weekend. I like food and culture, relaxed pace.”
Ask clarifying questions only when required (max 6).
Confirm constraints before generating the plan.
2. Voice-Based Editing
The user must be able to modify the itinerary using voice:

“Make Day 2 more relaxed.”
“Swap the Day 1 evening plan to something indoors.”
“Reduce travel time.”
“Add one famous local food place.”
Only the affected part of the itinerary should change.

3. Explanation & Reasoning
The assistant must answer:

“Why did you pick this place?”
“Is this plan doable?”
“What if it rains?”
Explanations must be grounded, not generic.

Companion UI
Your UI can be simple, but must include:

Day-wise itinerary (Day 1 / Day 2 / Day 3)
Morning / Afternoon / Evening blocks
Duration and estimated travel time between stops
A microphone button + live transcript
A “Sources” or “References” section showing where information came from
You may build the UI using Lovable or Figma Make.

Data Requirements
You must use publicly available datasets.

Recommended (you may standardize to these):

**OpenStreetMap (Overpass API)** – Points of Interest
Wikivoyage / Wikipedia – City guides and travel tips
Open-Meteo API (optional, bonus) – Weather forecasts
Rules:

POIs must map back to dataset records
Travel tips must come from RAG sources
If data is missing, the system must say so
MCP Integration
Your system must use at least two MCP tools in the orchestration layer.

Required MCP Tools (examples)
POI Search MCP Inputs: city, interests, constraints Outputs: ranked POIs with metadata
Itinerary Builder MCP Inputs: candidate POIs, daily time window, pace Outputs: structured day-wise itinerary
Optional MCP Tools (Bonus)
Travel Time Estimator MCP
Weather Adjustment MCP
You must demonstrate MCP calls clearly in your demo.

RAG Requirements
RAG must be used for:

Practical city guidance (areas to visit, safety, etiquette)
Explanations and justifications
Rules:

All factual tips must have citations
No hallucinated claims
Voice explanations can be short; citations must appear in UI
AI Evaluations
You must implement at least three evaluation checks:

Feasibility Eval

Daily duration ≤ available time
Reasonable travel times
Pace consistency
Edit Correctness Eval

Voice edits only modify intended sections
No unintended changes elsewhere
Grounding & Hallucination Eval

POIs map to dataset records
Tips cite RAG sources
Uncertainty is explicitly stated when data is missing
Evals can be rule-based or LLM-assisted but must be runnable.

Tech & Deployment Requirements
Build using LLM APIs
Voice input (speech-to-text required)
Version control using git
Deployed prototype (public URL)
Deliverables
You must submit:

Deployed application link

5 minute demo video showing:

Voice-based planning
Voice-based edit
Explanation (“why this plan?”)
Sources view
At least one eval running
Git repository with:

README (architecture + setup)
List of MCP tools used
Datasets referenced
How to run evals
Sample test transcripts
Scope Constraints
To keep this achievable:

Limit to one city
Max 2–4 day itinerary
Transit estimates can be heuristic
Focus on quality, not coverage
Evaluation Rubric
Voice UX & intent handling – 25%
MCP usage & system design – 20%
Grounding & RAG quality – 15%
AI evals & iteration depth – 20%
Workflow automation – 10%
Deployment & code quality – 10%
What We’re Looking For
We are not grading how fancy your UI is.

We are grading:

Can you design a tool-using AI system?
Do you understand LLM limitations?
Can you build evaluations and iterate?
Can you explain your decisions clearly?
This capstone mirrors how GenAI systems are built in real teams.