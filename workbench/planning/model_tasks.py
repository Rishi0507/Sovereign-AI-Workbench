"""Catalogue of model tasks: what each produces and which inputs it accepts."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ModelTaskSpec(BaseModel):
    name: str
    purpose: str
    schema_name: str | None
    output_type: str
    accepts: list[str] = Field(default_factory=lambda: ["*"])
    prompt: str
    description: str
    side_effect: bool = False


MODEL_TASKS: dict[str, ModelTaskSpec] = {s.name: s for s in [
    ModelTaskSpec(name="extract_findings", purpose="extract.findings", schema_name="findings",
                  output_type="findings", accepts=["document", "findings"], prompt="extract_findings",
                  description="Fill the inspection findings schema from document records"),
    ModelTaskSpec(name="draft_sections", purpose="draft.sections", schema_name="approval_note", output_type="note",
                  accepts=["findings", "check_results", "kb_passages", "graph_facts", "document"],
                  prompt="draft_sections", description="Draft cited approval note sections"),
    ModelTaskSpec(name="summarise_document", purpose="summarise.map", schema_name="contract_summary",
                  output_type="summary", accepts=["document"], prompt="summarise_map",
                  description="Map-reduce summary with page citations"),
    ModelTaskSpec(name="write_code", purpose="code.write", schema_name=None, output_type="code",
                  accepts=["*"], prompt="code_write", side_effect=True,
                  description="Write, run and fix a script in the sandbox (code-block protocol)"),
    ModelTaskSpec(name="compare_offers", purpose="compare.offers", schema_name="offer_comparison",
                  output_type="comparison", accepts=["tables", "kb_passages"], prompt="compare_offers",
                  description="Build a comparison table from extracted offer figures"),
    ModelTaskSpec(name="extract_calc_inputs", purpose="extract.calc_inputs", schema_name="calc_inputs",
                  output_type="calc_inputs", accepts=["document"], prompt="extract_calc_inputs",
                  description="Extract formula and inputs for a calculation"),
    ModelTaskSpec(name="chat_reply", purpose="chat.reply", schema_name="reply", output_type="reply",
                  accepts=["*"], prompt="chat_reply",
                  description="Reply to small talk or a request that does not say which document to use"),
    ModelTaskSpec(name="answer_question", purpose="answer.question", schema_name="answer", output_type="answer",
                  accepts=["kb_passages", "document", "graph_facts", "facts"], prompt="answer_question",
                  description="Answer a question from retrieved passages with citations"),
    ModelTaskSpec(name="draft_recommendation", purpose="draft.recommendation", schema_name="approval_note",
                  output_type="note", accepts=["comparison", "sandbox_result", "tables", "kb_passages"],
                  prompt="draft_recommendation", description="Draft a cited recommendation note"),
    ModelTaskSpec(name="outline_deck", purpose="deck.outline", schema_name="deck", output_type="deck",
                  accepts=["document"], prompt="deck_outline", description="Turn notes into a slide outline"),
]}

SCHEMA_TO_TASK = {"findings": "extract_findings", "approval_note": "draft_sections",
                  "contract_summary": "summarise_document", "offer_comparison": "compare_offers",
                  "calc_inputs": "extract_calc_inputs", "answer": "answer_question", "deck": "outline_deck"}

TOOL_OUTPUTS: dict[str, list[str]] = {
    "read_document": ["document", "tables", "findings"],
    "read_file": ["document"],
    "document_stats": ["facts"],
    "list_files": ["file_list"],
    "write_file": ["file"],
    "search_kb": ["kb_passages"],
    "graph_lookup": ["graph_facts"],
    "check_consistency": ["check_results"],
    "calculate": ["calc_result"],
    "run_python": ["sandbox_result"],
    "make_docx": ["file"],
    "make_xlsx": ["file"],
    "make_pptx": ["file"],
    "recall": ["record"],
    "delegate": ["delegated"],
    "finish": ["none"],
}
TOOL_ACCEPTS: dict[str, list[str]] = {
    "read_document": [],
    "read_file": [],
    "document_stats": [],
    "check_consistency": ["findings", "kb_passages", "graph_facts", "document"],
    "calculate": ["calc_inputs"],
}
RENDERERS = {"docx": ["make_docx"], "xlsx": ["make_xlsx"], "pptx": ["make_pptx"],
             "md": ["write_file"], "code": ["write_code", "run_python", "write_file"]}
