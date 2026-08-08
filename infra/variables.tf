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

variable "summarizer_categories" {
  description = "Category list the summarizer LLM classifies articles into"
  type        = list(string)
  default = [
    "国際", "政治", "経済", "ビジネス", "市場", "テクノロジー", "AI", "科学",
    "健康", "社会", "環境", "スポーツ", "文化", "エンタメ", "ライフスタイル", "話題",
  ]
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
