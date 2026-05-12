{{/*
Standard names. The release is expected to be installed once per
artifactory instance (one for sender on namespace artifactory-a, one
for receiver on namespace artifactory-b).
*/}}

{{- define "airlift.fullname" -}}
artifactory-airlift
{{- end -}}

{{- define "airlift.configMapName" -}}
{{ include "airlift.fullname" . }}-config
{{- end -}}

{{- define "airlift.secretName" -}}
{{- if .Values.artifactory.existingSecret -}}
{{ .Values.artifactory.existingSecret }}
{{- else -}}
{{ include "airlift.fullname" . }}-token
{{- end -}}
{{- end -}}

{{- define "airlift.statePvcName" -}}
{{ include "airlift.fullname" . }}-state
{{- end -}}

{{- define "airlift.spoolPvcName" -}}
{{ include "airlift.fullname" . }}-spool
{{- end -}}

{{- define "airlift.labels" -}}
app.kubernetes.io/name: artifactory-airlift
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: {{ .Values.mode }}
{{- end -}}
