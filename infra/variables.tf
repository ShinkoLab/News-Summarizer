variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
}

variable "region" {
  description = "Cloud Run, Firestore, Artifact Registry region"
  type        = string
  default     = "asia-northeast1"
}

variable "vertex_location" {
  description = "Vertex AI endpoint location"
  type        = string
  default     = "global"
}

variable "llm_model" {
  description = "Vertex AI model ID"
  type        = string
  default     = "gemini-3.1-flash-lite"
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
