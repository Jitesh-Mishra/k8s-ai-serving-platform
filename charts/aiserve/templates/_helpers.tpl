{{- define "aiserve.image" -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end }}
{{- define "aiserve.dbUrl" -}}postgresql://aiserve:{{ .Values.postgres.password }}@postgres:5432/aiserve{{- end }}
{{- define "aiserve.podEnv" -}}
- name: POD_NAME
  valueFrom: {fieldRef: {fieldPath: metadata.name}}
- name: POD_NAMESPACE
  valueFrom: {fieldRef: {fieldPath: metadata.namespace}}
- name: REDIS_URL
  value: redis://redis:6379/0
- name: DATABASE_URL
  value: {{ include "aiserve.dbUrl" . }}
{{- end }}
