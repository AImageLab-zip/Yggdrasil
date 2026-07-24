from django.urls import path

from common.consumers import LiveTranscriptionConsumer


websocket_urlpatterns = [
    path(
        "ws/live-transcription/<str:domain>/<int:patient_id>/",
        LiveTranscriptionConsumer.as_asgi(),
        name="live_transcription",
    ),
]
