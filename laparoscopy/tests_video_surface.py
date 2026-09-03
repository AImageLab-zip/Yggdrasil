"""The laparoscopy video surface, as the patient-detail page wires it.

Roadmap Phase 10 replaced 4,919 lines of Konva -- `laparoscopy_annotator.js` and its six
mixins -- with the `video-annotate` bundle entry, and removed both Konva CDN tags, so no
page in this repository loads Konva any more.

Phase 5's lesson is why this module exists: **a template id joining two files is an
untested interface**, and a rename leaves the JS holding `None` and a control stuck in
whatever state the template shipped -- silently, and only on the surface being replaced.

The other property asserted here is the one that decides whether a mask lands on the
right pixels: the page must state the video's frame size and frame rate, and must refuse
to mount the annotator when it cannot. A browser cannot read a video's frame rate, so
guessing 30 for a 25 fps recording would put every annotation on the wrong frame and look
entirely correct while doing it.
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from common.models import (
    AnnotationMethod,
    FileRegistry,
    Modality,
    Project,
    ProjectAccess,
)
from laparoscopy.models import TYPE_PALETTE, Folder, Patient, RegionType, RegionTypeUserColor

#: The ids `frontend/imaging/video/bootstrap.js` and `pageControls.js` look up by name.
#: `VIDEO_CONTROL_IDS` in that module is the other half of this list; a rename on either
#: side leaves the JS holding `None` and a control stuck in whatever state the template
#: shipped, silently and only on this surface.
CONTROL_IDS = (
    "videoAnnotateData",
    "video-annotate-viewport",
    "video-placeholder",
    "annotation-toolbar",
    "annotation-toggle-btn",
    "brush-size-input",
    "brush-size-label",
    "zoom-in-btn",
    "zoom-out-btn",
    "zoom-reset-btn",
    "save-annotations-btn",
    "savingIndicator",
    "frame-nav-bar",
    "frame-first",
    "frame-prev10",
    "frame-prev",
    "frame-play",
    "frame-next",
    "frame-next10",
    "frame-last",
    "frame-timestamp",
    "region-types-panel",
    "region-list",
    "toggle-regions-visibility-btn",
    "shapes-list-panel",
    "shapes-list",
    "shapes-filter-btn",
    "temporal-classification-bar",
    "timeline-track-wrap",
    "timeline-pins-layer",
    "timeline-segments-layer",
    "timeline-playhead",
    "timeline-current-time",
    "timeline-duration",
    "timeline-add-pin-btn",
    "timeline-class-list",
    "timeline-class-active-label",
    "timeline-class-active-swatch",
    "timeline-active-class",
    # The quadrant administration panel. Authored in Phase 10 and resolved by no
    # JavaScript at all until now, which is exactly the failure this list exists to
    # catch: the panel kept `d-none` forever and no quadrant could be created in the
    # page, so "Add Marker" could only ever refuse.
    "quadrant-types-panel",
    "timeline-add-class-btn",
    "timeline-class-admin-list",
)

#: The toolbar keys `frontend/imaging/video/editor.js` TOOL_PLAN answers to.
TOOL_KEYS = ("brush", "eraser", "polygon", "pan")


@override_settings(SECURE_SSL_REDIRECT=False)
class VideoSurfaceRenderTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="laparoscopy",
            defaults={"name": "Laparoscopy", "domain": "laparoscopy"},
        )
        self.modality, _ = Modality.objects.get_or_create(
            slug="video", defaults={"name": "Video"}
        )
        self.project.modalities.set([self.modality])
        # The toolbar is gated on the project enabling the method, and the write
        # endpoint re-checks it. Enabling it here exercises the real path; without it
        # the page renders correctly *and* without an annotator, which is a legitimate
        # state and not the one under test.
        method, _ = AnnotationMethod.objects.get_or_create(
            slug="video_regions",
            defaults={"name": "Video region annotation", "domain": "laparoscopy"},
        )
        self.project.annotation_methods.add(method)

        self.user = User.objects.create_user(username="lap-render", password="x")  # noqa: S106
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)

        self.folder = Folder.objects.create(name="Cases", project=self.project)
        self.patient = Patient.objects.create(
            name="Case", folder=self.folder, project=self.project
        )

    def _video(self, *, probe=True, file_type="video_raw", subtype="", size=(1920, 1080)):
        metadata = {"original_filename": "op.mp4"}
        if probe:
            metadata["probe"] = {
                "width": size[0],
                "height": size[1],
                "fps": 25.0,
                "frame_count": 500,
            }
        name = f"{file_type}{('-' + subtype) if subtype else ''}"
        return FileRegistry.objects.create(
            domain="laparoscopy",
            laparoscopy_patient=self.patient,
            file_type=file_type,
            subtype=subtype,
            file_path=f"laparoscopy/patient_{self.patient.patient_id}/{name}.mp4",
            file_size=1024,
            file_hash=f"videohash-{name}",
            modality=self.modality,
            metadata=metadata,
        )

    def _payload_text(self, html):
        start = html.index('id="videoAnnotateData"')
        return html[html.index(">", start) + 1 : html.index("</script>", start)].strip()

    def _annotation_track(self, *, probe=True, size=(1920, 1080)):
        """The subsampled derivative the annotator opens on.

        Annotation is gated on this existing: a raw recording runs at 25-30 fps, the
        record is one labelmap per annotated frame, and the export reads the subsampled
        track -- so strokes drawn on the raw video would describe frames nothing else
        can line up with. A patient with only a `video_raw` row plays but does not
        annotate, which is `video_state == 'processing'`.
        """
        return self._video(
            probe=probe, file_type="video_processed", subtype="subsampled", size=size
        )

    def _annotatable(self):
        """The ordinary ready state: an uploaded recording and its sampled track."""
        raw = self._video(probe=True)
        return raw, self._annotation_track()

    def _payload(self, html):
        import json

        start = html.index('id="videoAnnotateData"')
        body = html[html.index(">", start) + 1 : html.index("</script>", start)]
        return json.loads(body)

    def _page(self):
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()
        response = self.client.get(
            reverse(
                "laparoscopy:patient_detail",
                kwargs={"patient_id": self.patient.patient_id},
            )
        )
        return response, response.content.decode()

    # --- the wiring ---------------------------------------------------------------

    def test_every_id_the_surface_resolves_is_in_the_markup(self):
        self._video()
        _response, html = self._page()
        for element_id in CONTROL_IDS:
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_every_toolbar_key_the_tool_plan_answers_to_is_in_the_markup(self):
        self._video()
        _response, html = self._page()
        for key in TOOL_KEYS:
            self.assertIn(f'data-tool="{key}"', html, key)

    def test_the_video_entry_is_loaded(self):
        self._video()
        _response, html = self._page()
        self.assertIn("video-annotate", html)

    def test_konva_is_gone(self):
        """The last consumer went with Phase 10, and nothing must bring the tag back."""
        self._video()
        _response, html = self._page()
        self.assertNotIn("konva.min.js", html.lower())

    def test_the_deleted_annotator_is_not_referenced(self):
        self._video()
        _response, html = self._page()
        for script in (
            "laparoscopy_annotator.js",
            "laparoscopy_annotator_shapes.js",
            "laparoscopy_annotator_mask.js",
            "laparoscopy_annotator_magic.js",
            "laparoscopy_annotator_timeline.js",
            "laparoscopy_annotator_utils.js",
            "laparoscopy_annotator_api.js",
            "laparoscopy_annotator_worker.js",
        ):
            self.assertNotIn(script, html, script)

    # --- the payload, which decides which pixels a mask describes -------------------

    def test_the_payload_states_the_frame_size_and_rate_the_server_probed(self):
        self._video()
        track = self._annotation_track()
        _response, html = self._page()
        payload = self._payload(html)
        self.assertEqual(payload["width"], 1920)
        self.assertEqual(payload["height"], 1080)
        self.assertEqual(payload["fps"], 25.0)
        self.assertEqual(payload["frameCount"], 500)
        self.assertEqual(payload["patientId"], self.patient.patient_id)
        self.assertIn("video-annotations", payload["endpoint"])
        # ...and it describes the sampled track, which is the file being annotated.
        self.assertIn(f"/{track.id}/", payload["videoUrl"])

    def test_the_payload_describes_the_track_and_not_the_raw_recording(self):
        """The two have different frame rates, and picking the wrong one mis-files
        every mask while looking entirely correct. The raw runs at 25 fps here; the
        sampled track is one frame per source second."""
        self._video(probe=True, size=(1920, 1080))
        track = self._video(
            file_type="video_processed", subtype="subsampled", size=(1280, 720)
        )
        track.metadata = {**track.metadata, "probe": {
            "width": 1280, "height": 720, "fps": 1.0, "frame_count": 20,
        }}
        track.save(update_fields=["metadata"])

        _response, html = self._page()
        payload = self._payload(html)

        self.assertEqual(payload["fps"], 1.0)
        self.assertEqual(payload["frameCount"], 20)
        self.assertEqual(payload["width"], 1280)

    def test_a_recording_with_no_sampled_track_yet_reports_processing(self):
        """The gate the user asked for: no annotating a video the job has not finished.

        The recording is stored, probed and playable -- what is missing is the track
        annotations are recorded against, so the annotator is withheld and the page says
        the recording is still processing rather than that none was uploaded.
        """
        self._video(probe=True)

        response, html = self._page()

        self.assertEqual(response.context["video_state"], "processing")
        self.assertEqual(self._payload_text(html), "null")
        self.assertIn("still being", html)
        self.assertNotIn("No video uploaded for this patient.", html)
        # ...and it still plays.
        self.assertTrue(response.context["has_video"])
        self.assertTrue(response.context["video_url"])

    def test_a_video_with_no_recorded_probe_does_not_mount_the_annotator(self):
        """Rather than guess a frame rate, which would mis-file every mask.

        A browser cannot read a video's frame rate. Mounting anyway and defaulting to 30
        would put every annotation of a 25 fps recording on the wrong frame, and nothing
        in the interface would look wrong.
        """
        self._video(probe=True)
        self._annotation_track(probe=False)
        _response, html = self._page()
        self.assertIn("videoAnnotateData", html)
        self.assertEqual(self._payload_text(html), "null")

    def test_the_toolbar_is_hidden_when_the_project_disables_the_method(self):
        """The gate is the project registry, and the page has to honour it."""
        self.project.annotation_methods.clear()
        self._video()
        _response, html = self._page()
        self.assertNotIn('id="annotation-toggle-btn"', html)

    def test_a_patient_with_no_video_renders_without_the_surface(self):
        response, html = self._page()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="video-annotate-viewport"', html)

    def test_the_page_carries_a_csrf_input_for_the_writes_this_surface_makes(self):
        """`CSRF_USE_SESSIONS = True`, so there is no cookie and the input is the only
        source. Without it the annotation save and every quadrant-marker write is a bare
        403 -- which is the failure the maxillo grid already hit and documented."""
        self._video()
        _response, html = self._page()
        self.assertIn("csrfmiddlewaretoken", html)

    def test_the_glue_is_a_module_and_not_an_inline_script(self):
        """It polled `window.YggVideoAnnotate.surface` every 50 ms and wired the tool
        buttons and nothing else. `frontend/imaging/video/pageControls.js` is where the
        frame arithmetic and the tool/region rules can be tested."""
        self._video()
        _response, html = self._page()
        self.assertNotIn("YggVideoAnnotate?.surface", html)
        self.assertNotIn("whenMounted", html)

    # --- which row the page asks about, and what it says when it cannot ------------

    def test_the_page_uses_the_probed_row_even_when_a_derivative_outranks_it(self):
        """The regression that made a real patient read as having no video.

        `annotations_rasterize_video_masks` records a probe on the **video_raw** row.
        This page ranks a `video_processed`/`compressed` derivative first. Asking only
        the top-ranked row therefore found no probe on studies that had one, returned a
        `null` payload, and left the "No video uploaded for this patient." placeholder
        on screen over a stored, playable recording.
        """
        raw = self._video(probe=True)
        self._video(probe=False, file_type="video_processed", subtype="compressed")

        response, _html = self._page()

        # Playback, not annotation: the payload describes the sampled track, and this is
        # about which row the page *plays* when the better-ranked one cannot be described.
        self.assertEqual(response.context["video_file"].id, raw.id)
        self.assertIn(f"/{raw.id}/", response.context["video_url"])

    def test_a_probed_derivative_is_still_preferred_when_it_has_one(self):
        """The ranking is not abandoned, only made conditional on being answerable."""
        self._video(probe=True)
        compressed = self._video(
            probe=True, file_type="video_processed", subtype="compressed",
            size=(1280, 720),
        )
        response, _html = self._page()

        self.assertEqual(response.context["video_file"].id, compressed.id)
        self.assertIn(f"/{compressed.id}/", response.context["video_url"])

    def test_an_unprobed_video_says_so_instead_of_claiming_none_was_uploaded(self):
        self._video(probe=False)
        self._annotation_track(probe=False)
        response, html = self._page()
        self.assertEqual(response.context["video_state"], "unprobed")
        self.assertIn("has not", html)
        self.assertNotIn("No video uploaded for this patient.", html)

    def test_a_patient_with_no_video_still_says_no_video_was_uploaded(self):
        response, html = self._page()
        self.assertEqual(response.context["video_state"], "absent")
        self.assertIn("No video uploaded for this patient.", html)

    def test_a_ready_video_never_says_none_was_uploaded(self):
        """The placeholder is what the reader sees until the surface mounts, and it used
        to share the `absent` sentence. Saying a stored, playable recording was never
        uploaded sends somebody looking for an upload that already happened -- which is
        exactly what the HTTP 500 on the state endpoint made the page do."""
        self._annotatable()
        response, html = self._page()
        self.assertEqual(response.context["video_state"], "ready")
        self.assertNotIn("No video uploaded for this patient.", html)
        self.assertIn("Loading the recording", html)

    def test_a_mountable_video_reports_ready(self):
        self._annotatable()
        response, _html = self._page()
        self.assertEqual(response.context["video_state"], "ready")

    def test_an_absent_video_says_what_the_server_looked_for(self):
        """"No video uploaded" over a file in the bucket is a claim; staff see the
        working, because the alternative is a round trip to find out."""
        FileRegistry.objects.create(
            domain="laparoscopy",
            laparoscopy_patient=self.patient,
            file_type="audio_raw",
            file_path="laparoscopy/audio.wav",
            file_size=1,
            file_hash="a",
        )
        response, html = self._page()
        self.assertEqual(response.context["video_state"], "absent")
        self.assertIn("audio_raw", html)
        self.assertIn("not registered", html)

    def test_a_patient_with_no_files_at_all_says_so(self):
        response, _html = self._page()
        self.assertIn("no registered files at all", response.context["video_diagnosis"])

    def test_the_diagnosis_names_the_row_being_played_and_any_without_a_probe(self):
        raw = self._video(probe=True)
        self._video(probe=False, file_type="video_processed", subtype="compressed")
        response, _html = self._page()
        diagnosis = response.context["video_diagnosis"]
        self.assertIn("no probe", diagnosis)
        self.assertIn(f"Playing #{raw.id}", diagnosis)


@override_settings(SECURE_SSL_REDIRECT=False)
class VideoAnnotationStateEndpointTests(VideoSurfaceRenderTests):
    """The endpoint the page names, exercised over the wire.

    This is the gap that let a `TypeError` reach production. `video_regions_state` is
    covered at the service layer by `annotations/tests_video.py` and the surface test
    above only asserts that the endpoint's *URL* appears in the payload -- so the lines
    that sit between the service and the wire, which is where the defect was, had never
    been executed. The annotator refuses to mount on any non-OK response, so a 500 here
    is a page with no video on it.
    """

    def _state(self):
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()
        # No namespace: `laparoscopy.urls` is included without one, and the
        # `laparoscopy:` prefix belongs to the maxillo app urls it re-includes. Same
        # note as `_video_annotate_payload`, which got this wrong once and turned a bad
        # URL name into "this patient has no video".
        return self.client.get(
            reverse(
                "patient_video_annotations",
                kwargs={"patient_id": self.patient.patient_id},
            )
        )

    def test_the_state_endpoint_answers_a_patient_with_no_annotations(self):
        self._video()
        response = self._state()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["regionTypes"], [])

    def test_the_state_endpoint_lists_the_project_region_types(self):
        """`_types_payload` returns the list itself; the view used to index it with
        "types", the key `_handle_type_list` wraps it in on the way out. That raised
        `TypeError: list indices must be integers` on every GET."""
        self._video()
        RegionType.objects.create(project=self.project, name="Liver", color="#aa0000", order=1)
        RegionType.objects.create(project=self.project, name="Gallbladder", color="#00aa00", order=0)

        response = self._state()
        self.assertEqual(response.status_code, 200)
        types = response.json()["regionTypes"]
        self.assertEqual([t["name"] for t in types], ["Gallbladder", "Liver"])
        self.assertEqual(types[1]["color"], "#aa0000")

    def test_a_user_preference_overrides_the_project_colour(self):
        self._video()
        region = RegionType.objects.create(project=self.project, name="Liver", color="#aa0000")
        RegionTypeUserColor.objects.create(region_type=region, user=self.user, color="#0000ff")

        types = self._state().json()["regionTypes"]
        self.assertEqual(types[0]["color"], "#0000ff")

    def test_the_state_carries_what_the_annotator_needs_to_mount(self):
        """The mount reads `frames` into the store and `width`/`height` decide which
        pixels a stored mask describes; a response missing them declines rather than
        drawing masks over the wrong picture."""
        self._video()
        state = self._state().json()
        for key in ("frames", "revision", "regionTypes"):
            self.assertIn(key, state)


class RegionTypeManagementTests(VideoSurfaceRenderTests):
    """The region-type endpoints, as the panel now calls them.

    Rename, recolour and delete have existed since Phase 10 and the page called none of
    them: the panel offered a name and nothing else, so a region typed wrong stayed wrong
    and one created by mistake stayed forever. These pin the contract the new per-region
    controls depend on.
    """

    def _session(self):
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def _post(self, body):
        self._session()
        return self.client.post(
            reverse("region_types"), data=body, content_type="application/json"
        )

    def _patch(self, pk, body):
        self._session()
        return self.client.patch(
            reverse("region_type_detail", kwargs={"pk": pk}),
            data=body,
            content_type="application/json",
        )

    def test_each_new_region_type_gets_a_colour_of_its_own(self):
        """Every region used to be born in the model default, so a project's second
        region was the same blue as its first and the masks on screen were told apart
        only by which one happened to be drawn last."""
        colors = [self._post({"name": name}).json()["color"] for name in ("A", "B", "C")]

        self.assertEqual(len(set(colors)), 3, colors)
        self.assertEqual(colors[0], TYPE_PALETTE[0])
        self.assertEqual(colors[1], TYPE_PALETTE[1])

    def test_a_colour_freed_by_a_delete_is_handed_out_again(self):
        first = self._post({"name": "A"}).json()
        self._post({"name": "B"})
        self._session()
        self.assertEqual(
            self.client.delete(
                reverse("region_type_detail", kwargs={"pk": first["id"]})
            ).status_code,
            204,
        )

        # Counted off the rows, not off `order`: counting would skip the freed colour
        # for the life of the project.
        self.assertEqual(self._post({"name": "C"}).json()["color"], first["color"])

    def test_a_stated_colour_still_wins_over_the_palette(self):
        self.assertEqual(self._post({"name": "A", "color": "#123456"}).json()["color"], "#123456")

    def test_a_region_type_can_be_renamed_and_recoloured(self):
        created = self._post({"name": "Liver"}).json()

        renamed = self._patch(created["id"], {"name": "Fegato", "color": "#0000ff"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["name"], "Fegato")
        # The colour is stored per user, so the project default is untouched and the
        # response is the effective one.
        self.assertEqual(renamed.json()["color"], "#0000ff")
        self.assertEqual(RegionType.objects.get(pk=created["id"]).name, "Fegato")

    def test_a_rename_onto_an_existing_name_is_refused_rather_than_merged(self):
        first = self._post({"name": "Liver"}).json()
        self._post({"name": "Gallbladder"})

        conflict = self._patch(first["id"], {"name": "Gallbladder"})
        self.assertEqual(conflict.status_code, 400)
        self.assertIn("already exists", conflict.json()["error"])


class VideoRegionToolAttributionTests(VideoSurfaceRenderTests):
    """The tool that wrote a mask survives a save and a reload.

    The annotation list names it, so it has to be part of the record rather than of the
    session. Masks stored before this existed carry nothing, and nothing is invented for
    them -- the tool was never recorded anywhere.
    """

    def _endpoint(self):
        return reverse(
            "patient_video_annotations", kwargs={"patient_id": self.patient.patient_id}
        )

    def _save(self, regions):
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()
        return self.client.put(
            self._endpoint(),
            data={
                "width": 4,
                "height": 3,
                "expectedRevision": 0,
                "frames": [{"timeMs": 120, "regions": regions}],
                "prompts": [],
            },
            content_type="application/json",
        )

    def test_the_tool_round_trips_through_a_save(self):
        self._annotatable()
        RegionType.objects.create(project=self.project, name="Liver")

        saved = self._save({"Liver": {"rle": [0, 1, 11], "tool": "polygon"}})
        self.assertEqual(saved.status_code, 200, saved.content)

        frames = self.client.get(self._endpoint()).json()["frames"]
        self.assertEqual(frames[0]["regions"]["Liver"]["tool"], "polygon")

    def test_a_mask_saved_without_a_tool_reads_back_without_one(self):
        self._annotatable()
        RegionType.objects.create(project=self.project, name="Liver")

        self.assertEqual(self._save({"Liver": {"rle": [0, 1, 11]}}).status_code, 200)

        entry = self.client.get(self._endpoint()).json()["frames"][0]["regions"]["Liver"]
        # Absent, not null: "the record does not say" is a different statement from
        # "no tool", and the list renders the difference.
        self.assertNotIn("tool", entry)

    def test_a_tool_the_toolbar_does_not_have_is_refused(self):
        """The value is shown to readers, so an arbitrary string would render as a tool
        that does not exist."""
        self._annotatable()
        RegionType.objects.create(project=self.project, name="Liver")

        refused = self._save({"Liver": {"rle": [0, 1, 11], "tool": "airbrush"}})
        self.assertEqual(refused.status_code, 400, refused.content)

    def test_a_save_is_refused_while_the_recording_is_still_processing(self):
        """The gate is re-checked on the wire, not only in the template.

        The page withholds the annotator until the sampled track exists, but a tab
        opened before the job finished still holds a mounted editor, and the endpoint is
        reachable directly. 409 rather than 403: nothing is wrong with the request or
        the caller, the state it needs has not arrived yet.
        """
        self._video(probe=True)
        RegionType.objects.create(project=self.project, name="Liver")

        refused = self._save({"Liver": {"rle": [0, 1, 11]}})

        self.assertEqual(refused.status_code, 409, refused.content)
        self.assertTrue(refused.json()["video_processing"])

    def test_the_payload_carries_the_compressed_film_to_watch(self):
        """Watching and annotating are two films, and the payload names both.

        The annotated track is one frame per source second, so pressing play on it steps
        through stills -- "it plays the cut frames" was the report. `playbackUrl` is the
        compressed derivative of the same surgery at its real frame rate; the surface
        watches that and keeps filing every mask against a frame of the sampled track,
        which is what the export reads.
        """
        self._video(probe=True)
        track = self._annotation_track()
        compressed = self._video(
            probe=True, file_type="video_processed", subtype="compressed"
        )

        _response, html = self._page()
        payload = self._payload(html)
        self.assertIn(f"/{track.id}/", payload["videoUrl"])
        self.assertIn(f"/{compressed.id}/", payload["playbackUrl"])
        self.assertNotEqual(payload["videoUrl"], payload["playbackUrl"])

    def test_one_film_is_watched_where_there_is_only_one(self):
        """No compressed derivative means no second film, and the surface says so by
        watching the track it annotates -- not by playing `null`."""
        self._video(probe=True)
        self._annotation_track()

        _response, html = self._page()
        payload = self._payload(html)
        # The best playable file here *is* the sampled track or the raw recording; either
        # way the payload must not name a second film that does not exist.
        self.assertIsNone(payload["playbackUrl"])
