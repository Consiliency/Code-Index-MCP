class ChunkInput {
  chunk_id: string,
  symbol: string,
  node_type: string,
  line_start: int,
  line_end: int,
  content: string,
}

class ChunkSummary {
  chunk_id: string,
  summary: string,
}

class FileSummaryResult {
  summaries: ChunkSummary[],
}

function SummarizeFileChunks(
  language: string,
  file_path: string,
  file_content: string,
  chunks: ChunkInput[],
) -> FileSummaryResult {
  client: openai.GenericClient.new(
    model = "gpt-oss-120b",
    base_url = "https://api.cerebras.ai/v1",
    api_key = env.CEREBRAS_API_KEY,
    max_tokens = 4096,
  )
  prompt: `
    ${role("system")}
    You are a concise code documentation assistant.
    For each code chunk listed below, write 1-2 sentences describing what it does
    and what it returns, produces, or causes, including side effects. Mention key
    parameters only when they meaningfully affect behavior. Do not output code.
    ${ctx.output_format()}

    ${role("user")}
    Language: ${language}
    File: ${file_path}

    Full source file:
    ~~~${language}
    ${file_content}
    ~~~

    Summarize each chunk from that file. Return one ChunkSummary per chunk,
    using the exact chunk_id provided.

    ${for (let chunk in chunks)}
    ---
    chunk_id: ${chunk.chunk_id}
    symbol: ${chunk.symbol}
    node_type: ${chunk.node_type}
    lines: ${chunk.line_start}-${chunk.line_end}
    ~~~${language}
    ${chunk.content}
    ~~~
    ${endfor}
  `
}

function SummarizeChunkAlone(
  language: string,
  symbol: string,
  line_start: int,
  line_end: int,
  content: string,
  parent_context: string,
  file_context: string,
) -> ChunkSummary {
  client: openai.GenericClient.new(
    model = "gpt-oss-120b",
    base_url = "https://api.cerebras.ai/v1",
    api_key = env.CEREBRAS_API_KEY,
    max_tokens = 4096,
  )
  prompt: `
    ${role("system")}
    You are a concise code documentation assistant.
    Write 1-2 sentences describing what the code chunk does and what it returns,
    produces, or causes, including side effects. Mention key parameters only
    when they meaningfully affect behavior. Do not output code.
    ${ctx.output_format()}

    ${role("user")}
    Language: ${language}
    ${file_context}
    ${parent_context}
    Code chunk '${symbol}' (lines ${line_start}-${line_end}):
    ~~~${language}
    ${content}
    ~~~

    Set chunk_id to "${symbol}" in your response.
  `
}
