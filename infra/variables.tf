variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
}

variable "region" {
  description = "Cloud Run, Firestore, Artifact Registry region"
  type        = string
  default     = "asia-northeast1"
}

variable "llm_base_url" {
  description = "OpenAI-compatible LLM endpoint base URL"
  type        = string
}

variable "llm_model" {
  description = "LLM model ID"
  type        = string
}

variable "llm_provider" {
  description = <<-EOT
    LLM provider / API shape ("openai" = Chat Completions, "openai_responses" = OpenAI Responses API).
    Some OpenCode Zen models (e.g. gpt-5.6-luna, grok-4.6) are only exposed via /v1/responses,
    not /v1/chat/completions — see infra/DEPLOYMENT.md for the incident this was added for.
  EOT
  type        = string
  default     = "openai"

  validation {
    # config.py の LLMConfig.provider が Literal["openai", "vertex", "openai_responses"]。
    # ここで弾かないと typo した値がそのままCloud Runに渡り、全実行がpydanticの
    # ValidationErrorで起動直後に失敗する（#11と同種の「気づきにくい全滅」になる）。
    condition     = contains(["openai", "vertex", "openai_responses"], var.llm_provider)
    error_message = "llm_provider は openai / vertex / openai_responses のいずれかを指定してください（config.py の Literal 制約に合わせる）。"
  }
}

variable "llm_structured_output" {
  description = "Whether the endpoint honors OpenAI Structured Output (response_format / text_format)"
  type        = bool
  default     = false
}

variable "llm_thinking" {
  description = "Whether llm_model is a reasoning model (gates disable_temperature_with_thinking)"
  type        = bool
  default     = false
}

variable "llm_disable_temperature_with_thinking" {
  description = "Strip temperature from every step's parameters when llm_thinking is true (reasoning models that reject temperature)"
  type        = bool
  default     = false
}

variable "embedding_base_url" {
  description = "OpenAI-compatible embedding API endpoint base URL (used for similarity-based grouping)"
  type        = string
  default     = "https://openrouter.ai/api/v1"
}

variable "embedding_model" {
  description = "Embedding model ID"
  type        = string
  default     = "openai/text-embedding-3-small"
}

variable "llm_temperature" {
  description = "Global default temperature for llm.parameters"
  type        = number
  default     = 0.7
}

variable "llm_max_tokens" {
  description = "Global default max_tokens for llm.parameters"
  type        = number
  default     = 8192
}

variable "grouper_temperature" {
  description = "temperature override for the grouper step"
  type        = number
  default     = 0.2
}

variable "grouper_reasoning_effort" {
  description = "reasoning_effort override for the grouper step (reasoning models only)"
  type        = string
  default     = "low"
}

variable "summarizer_temperature" {
  description = "temperature override for the summarizer step"
  type        = number
  default     = 0.3
}

variable "summarizer_reasoning_effort" {
  description = "reasoning_effort override for the summarizer step (reasoning models only)"
  type        = string
  default     = "low"
}

variable "summarizer_individual_max_length" {
  description = "Character limit for each per-article summary (feeds the digest)"
  type        = number
  default     = 500
}

variable "max_articles_per_run" {
  description = "1回の実行で要約する記事数の上限。miniflux_fetch_limit がこれを下回ると、そちらが実際の天井になる"
  type        = number
  default     = 100

  validation {
    # config.py の SummarizerConfig.max_articles_per_run が Field(ge=1, le=200)。
    # 超過するとジョブが pydantic の ValidationError で起動直後に落ちるため、
    # apply の時点で弾く。
    condition     = var.max_articles_per_run >= 1 && var.max_articles_per_run <= 200
    error_message = "max_articles_per_run は 1〜200 の範囲で指定してください（config.py の Field 制約に合わせる）。"
  }
}

variable "miniflux_fetch_limit" {
  description = "Miniflux /v1/entries に渡す limit。重複除去で1割前後が落ちるため max_articles_per_run より多めにする"
  type        = number
  default     = 100

  validation {
    # Miniflux 側の MaxEntryLimit（internal/model/entry.go）が 1000。
    condition     = var.miniflux_fetch_limit >= 1 && var.miniflux_fetch_limit <= 1000
    error_message = "miniflux_fetch_limit は 1〜1000 の範囲で指定してください（Miniflux の MaxEntryLimit）。"
  }
}

variable "grouper_similarity_threshold" {
  description = "Cosine similarity threshold for embedding-based grouping"
  type        = number
  default     = 0.7
}

variable "digest_reasoning_effort" {
  description = "reasoning_effort override for the digest step (reasoning models only)"
  type        = string
  default     = "medium"
}

variable "summarizer_image" {
  description = "Digest-pinned or commit-tagged Summarizer container image"
  type        = string
}

variable "viewer_image" {
  description = "Digest-pinned or commit-tagged Viewer container image"
  type        = string
}

variable "viewer_users" {
  description = "Google account email addresses allowed through IAP"
  type        = set(string)
}

variable "viewer_domain" {
  description = "Custom domain (e.g. viewer.example.com) to map to the Viewer via Cloud Run Domain Mapping. Empty string (default) skips domain mapping entirely"
  type        = string
  default     = ""
}

variable "schedule" {
  description = "Cloud Scheduler cron expression"
  type        = string
  default     = "0 7 * * *"
}

variable "schedule_time_zone" {
  type    = string
  default = "Asia/Tokyo"
}

variable "miniflux_base_url" {
  type = string
}

variable "email_host" {
  type = string
}

variable "email_port" {
  type    = number
  default = 995
}

variable "email_username" {
  type = string
}

variable "email_max_fetch_attempts" {
  description = "保存に至らないメールをこの回数で打ち切り、処理済み扱いにする（poison message対策）"
  type        = number
  default     = 3
}

variable "email_delete_after_processing" {
  description = "DB保存に成功したメールと打ち切ったメールをPOP3サーバから削除するか"
  type        = bool
  default     = false
}

variable "billing_account_id" {
  description = "Optional billing account ID for a monthly budget alert"
  type        = string
  default     = ""
}

variable "monthly_budget_jpy" {
  type    = number
  default = 2000
}
