"""Public input compatibility; deprecated inputs are accepted, never executed."""
from ._native import DEPRECATED  # Shared compatibility field inventory.

TOOL_DESCRIPTION = '''The codex_images tool generates images directly through Codex Images using ChatGPT OAuth.
String prompts are sent verbatim; labeled-spec objects become deterministic Label: value lines.
Lists run up to four requests concurrently. variants repeats a prompt count times.
With an anchor, branded-parallel references the generated anchor; chain references anchor and previous output.
transparent requests native alpha, without chroma fallback. JPEG flattens alpha onto white.
Model, quality and size are server-managed on this subscription route. Requests dispatch gpt-image-2,
quality auto; requested size is only a hint. Final dimensions come from output.size_policy post-processing.
Deprecated rewriting, skill, chroma, model-selection and output_dir fields are ignored with warnings.
Returns codex-images.result/v2 with private image artifact resources; use codex_images_artifact for retrieval via mcp://codex-images/ resources.'''

TOOL_INPUT_SCHEMA = {
    'type': 'object', 'required': ['prompt'], 'additionalProperties': False,
    'properties': {
        'prompt': {'oneOf': [{'type': 'string'}, {'type': 'object'}, {'type': 'array', 'items': {'type': ['string', 'object']}}]},
        'batch_mode': {'type': 'string', 'enum': ['auto', 'single', 'parallel', 'variants', 'chain', 'branded-parallel']},
        'count': {'type': 'integer', 'minimum': 1},
        'anchor': {'type': ['string', 'object', 'null']},
        'references': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Up to five PNG/JPEG/WebP references per endpoint request, including generated anchor/previous images.'},
        'mask': {'type': ['string', 'object', 'null'], 'description': 'Edit mask, using the same image source forms as references. Requires a reference.'},
        'size': {'type': 'string', 'description': 'Server-managed size hint; final sizing uses output.'},
        'output_format': {'type': 'string', 'enum': ['png', 'jpeg', 'webp']},
        'transparent': {'type': 'boolean', 'description': 'Request native transparent background; warn if result is fully opaque.'},
        'chain_mode': {'description': 'Compatibility field; chain always references anchor and previous output.'},
    },
}
for field in DEPRECATED:
    TOOL_INPUT_SCHEMA['properties'][field] = {'description': f'Deprecated: {field} is ignored; prompts are sent verbatim.'}
for field in ('chroma_despill', 'chroma_despill_mode', 'chroma_edge_erode_px', 'chroma_auto_key', 'chroma_feather', 'chroma_spill_strength', 'vars'):
    TOOL_INPUT_SCHEMA['properties'][field] = {'description': 'Deprecated compatibility field; ignored.'}
