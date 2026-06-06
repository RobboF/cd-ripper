{{- define "cd-ripper.name" -}}
{{- .Chart.Name }}
{{- end }}

{{- define "cd-ripper.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "cd-ripper.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "cd-ripper.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "cd-ripper.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cd-ripper.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
