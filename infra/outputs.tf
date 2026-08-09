output "artifact_registry_repository" {
  value = google_artifact_registry_repository.containers.name
}

output "viewer_url" {
  value = google_cloud_run_v2_service.viewer.uri
}

output "summarizer_job" {
  value = google_cloud_run_v2_job.summarizer.name
}
