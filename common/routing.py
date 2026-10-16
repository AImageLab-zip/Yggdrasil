from django.urls import path

from common.consumers import AssistSessionConsumer, LiveTranscriptionConsumer


websocket_urlpatterns = [
    path(
        "ws/live-transcription/<str:domain>/<int:patient_id>/",
        LiveTranscriptionConsumer.as_asgi(),
        name="live_transcription",
    ),
    path(
        "ws/assist/<str:domain>/<int:patient_id>/",
        AssistSessionConsumer.as_asgi(),
        name="assist_session",
    ),
]
