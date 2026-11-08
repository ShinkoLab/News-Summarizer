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

output "agent_url" {
  description = "news-agent (agent-only Viewer: /mcp and /api/v1, no IAP) default URL; null when agent_enabled = false"
  value       = try(google_cloud_run_v2_service.news_agent[0].uri, null)
}

output "agent_domain_mapping_records" {
  description = "DNS records for agent_domain (populated after apply + Google-side validation); null when agent_domain is unset"
  value       = try(google_cloud_run_domain_mapping.agent[0].status, null)
}

output "summarizer_job" {
  value = google_cloud_run_v2_job.summarizer.name
}
