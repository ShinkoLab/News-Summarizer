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

variable "billing_account_id" {
  description = "Optional billing account ID for a monthly budget alert"
  type        = string
  default     = ""
}

variable "monthly_budget_jpy" {
  type    = number
  default = 2000
}
