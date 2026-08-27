output "artifact_registry_repository" {
  value = google_artifact_registry_repository.containers.name
}

output "viewer_url" {
  value = google_cloud_run_v2_service.viewer.uri
}

output "viewer_domain_mapping_records" {
  description = "DNS records to create at your registrar (populated after apply + Google-side validation); null when viewer_domain is unset"
  value       = try(google_cloud_run_domain_mapping.viewer[0].status, null)
}

output "summarizer_job" {
  value = google_cloud_run_v2_job.summarizer.name
}
