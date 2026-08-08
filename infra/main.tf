locals {
  required_services = toset([
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "firestore.googleapis.com",
    "iap.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each           = local.required_services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "containers" {
  location      = var.region
  repository_id = "news-summarizer"
  format        = "DOCKER"
  description   = "News Summarizer Cloud Run images"

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent-versions"
    action = "KEEP"
    most_recent_versions {
      keep_count = 3
    }
  }

  cleanup_policies {
    id     = "delete-old-versions"
    action = "DELETE"
    condition {
      older_than = "1209600s"
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_firestore_database" "default" {
  project                           = var.project_id
  name                              = "(default)"
  location_id                       = var.region
  type                              = "FIRESTORE_NATIVE"
  delete_protection_state           = "DELETE_PROTECTION_ENABLED"
  deletion_policy                   = "ABANDON"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_DISABLED"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "summarizer" {
  account_id   = "news-summarizer-job"
  display_name = "News Summarizer Cloud Run Job"
}

resource "google_service_account" "viewer" {
  account_id   = "news-viewer-service"
  display_name = "News Viewer Cloud Run Service"
}

resource "google_service_account" "scheduler" {
  account_id   = "news-summarizer-scheduler"
  display_name = "News Summarizer Scheduler Invoker"
}

resource "google_project_iam_member" "summarizer_datastore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.summarizer.email}"
}

resource "google_project_iam_member" "viewer_datastore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.viewer.email}"
}

resource "google_secret_manager_secret" "miniflux_api_key" {
  secret_id = "news-miniflux-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "email_password" {
  secret_id = "news-email-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "discord_webhook_url" {
  secret_id = "news-discord-webhook-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "llm_api_key" {
  secret_id = "news-llm-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "summarizer_secrets" {
  for_each = {
    miniflux = google_secret_manager_secret.miniflux_api_key.id
    email    = google_secret_manager_secret.email_password.id
    discord  = google_secret_manager_secret.discord_webhook_url.id
    llm      = google_secret_manager_secret.llm_api_key.id
  }
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.summarizer.email}"
}

resource "google_cloud_run_v2_job" "summarizer" {
  name                = "news-summarizer"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.summarizer.email
      timeout         = "3600s"
      max_retries     = 0

      containers {
        image = var.summarizer_image

        resources {
          limits = {
            cpu    = "1"
            memory = "2Gi"
          }
        }

        env {
          name  = "DATABASE_BACKEND"
          value = "firestore"
        }
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "LLM_PROVIDER"
          value = "openai"
        }
        env {
          name  = "LLM_BASE_URL"
          value = var.llm_base_url
        }
        env {
          name  = "LLM_MODEL"
          value = var.llm_model
        }
        env {
          name  = "LLM_STRUCTURED_OUTPUT"
          value = "false"
        }
        env {
          name  = "LLM_MAX_RETRIES"
          value = "1"
        }
        env {
          name  = "MAX_ARTICLES_PER_RUN"
          value = "100"
        }
        env {
          name  = "SUMMARIZER_CATEGORIES"
          value = join(",", var.summarizer_categories)
        }
        env {
          name  = "MINIFLUX_BASE_URL"
          value = var.miniflux_base_url
        }
        env {
          name  = "EMAIL_HOST"
          value = var.email_host
        }
        env {
          name  = "EMAIL_PORT"
          value = tostring(var.email_port)
        }
        env {
          name  = "EMAIL_USERNAME"
          value = var.email_username
        }
        env {
          name  = "EMAIL_USE_SSL"
          value = "true"
        }
        env {
          name  = "LOG_LEVEL"
          value = "INFO"
        }

        env {
          name = "MINIFLUX_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.miniflux_api_key.secret_id
              version = "latest"
            }
          }
        }
        env {
          name = "EMAIL_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.email_password.secret_id
              version = "latest"
            }
          }
        }
        env {
          name = "DISCORD_WEBHOOK_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.discord_webhook_url.secret_id
              version = "latest"
            }
          }
        }
        env {
          name = "LLM_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.llm_api_key.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_firestore_database.default,
    google_secret_manager_secret_iam_member.summarizer_secrets,
  ]
}

resource "google_cloud_run_v2_service" "viewer" {
  name                = "news-viewer"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = false

  template {
    service_account = google_service_account.viewer.email
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = var.viewer_image
      ports {
        container_port = 3000
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = "(default)"
      }
    }
  }

  depends_on = [google_firestore_database.default]
}

resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.viewer.location
  name     = google_cloud_run_v2_service.viewer.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

resource "google_iap_web_cloud_run_service_iam_binding" "family" {
  project                = var.project_id
  location               = google_cloud_run_v2_service.viewer.location
  cloud_run_service_name = google_cloud_run_v2_service.viewer.name
  role                   = "roles/iap.httpsResourceAccessor"
  members                = [for email in var.viewer_users : "user:${email}"]

  depends_on = [google_cloud_run_v2_service_iam_member.iap_invoker]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_job.summarizer.location
  name     = google_cloud_run_v2_job.summarizer.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "summarizer" {
  name             = "news-summarizer-daily"
  description      = "Run the personal news summarizer"
  region           = var.region
  schedule         = var.schedule
  time_zone        = var.schedule_time_zone
  attempt_deadline = "180s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.summarizer.name}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoker]
}

resource "google_billing_budget" "monthly" {
  count           = var.billing_account_id == "" ? 0 : 1
  billing_account = var.billing_account_id
  display_name    = "News Summarizer monthly budget"

  budget_filter {
    projects = ["projects/${data.google_project.current.number}"]
  }

  amount {
    specified_amount {
      currency_code = "JPY"
      units         = tostring(var.monthly_budget_jpy)
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

}
