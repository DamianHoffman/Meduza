// Command voice-service is Meduza's text-to-speech microservice.
//
// It wraps the ElevenLabs API behind a small internal HTTP endpoint so the
// voice integration is a clean service boundary: app.py (Python) calls
// POST /tts here instead of talking to ElevenLabs directly. This process
// owns the ElevenLabs credential — the Python side never sees it.
//
// Run it:
//
//	export ELEVENLABS_API_KEY=...   # or put it in voice-service/.env
//	go run .
//
// Standard library only, on purpose: this sandbox (and possibly yours)
// doesn't have network access to the Go module proxy, and a stateless
// single-endpoint service like this doesn't need a router or an HTTP
// client library to justify the dependency.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	// A premade ElevenLabs voice ("Rachel"), used as a safe out-of-the-box
	// default — same fallback the original Python implementation used.
	defaultVoiceID = "21m00Tcm4TlvDq8ikWAM"
	modelID        = "eleven_multilingual_v2"
	maxTextLength  = 2000
)

// elevenLabsBaseURL is overridable via ELEVENLABS_BASE_URL so it can be
// pointed at a local mock server for testing instead of the real API.
func elevenLabsBaseURL() string {
	if v := os.Getenv("ELEVENLABS_BASE_URL"); v != "" {
		return v
	}
	return "https://api.elevenlabs.io"
}

type ttsRequest struct {
	Text    string `json:"text"`
	VoiceID string `json:"voice_id,omitempty"`
}

type elevenLabsRequestBody struct {
	Text          string             `json:"text"`
	ModelID       string             `json:"model_id"`
	VoiceSettings map[string]float64 `json:"voice_settings"`
}

func writeJSONError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message})
}

// isValidVoiceID restricts voice_id to what a real ElevenLabs voice ID
// looks like (e.g. "21m00Tcm4TlvDq8ikWAM"): plain alphanumeric, no slashes,
// dots, or other characters that have any special meaning in a URL path.
var voiceIDPattern = regexp.MustCompile(`^[a-zA-Z0-9]{1,64}$`)

func isValidVoiceID(id string) bool {
	return voiceIDPattern.MatchString(id)
}

func ttsHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSONError(w, http.StatusMethodNotAllowed, "use POST")
		return
	}

	var req ttsRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	req.Text = strings.TrimSpace(req.Text)
	if req.Text == "" {
		writeJSONError(w, http.StatusBadRequest, "text is required")
		return
	}
	if len(req.Text) > maxTextLength {
		writeJSONError(w, http.StatusBadRequest, "text is too long")
		return
	}

	apiKey := os.Getenv("ELEVENLABS_API_KEY")
	if apiKey == "" {
		// Mirrors the original Python service's contract: missing
		// credentials is a 503 ("not configured yet"), not a 500.
		writeJSONError(w, http.StatusServiceUnavailable, "ELEVENLABS_API_KEY is not set")
		return
	}

	voiceID := req.VoiceID
	if voiceID == "" {
		voiceID = os.Getenv("ELEVENLABS_VOICE_ID")
	}
	if voiceID == "" {
		voiceID = defaultVoiceID
	}
	if !isValidVoiceID(voiceID) {
		// voice_id becomes part of the outbound URL's path — reject anything
		// that isn't a plain alphanumeric ID before it gets anywhere near
		// fmt.Sprintf. Nothing in this app currently sends a custom voice_id
		// (the frontend only ever sends `text`), but the field exists in the
		// request contract, so it gets validated regardless of who calls it.
		writeJSONError(w, http.StatusBadRequest, "invalid voice_id")
		return
	}

	audio, upstreamStatus, err := fetchSpeech(apiKey, voiceID, req.Text)
	if err != nil {
		log.Println("elevenlabs request failed:", err)
		writeJSONError(w, http.StatusBadGateway, "could not reach ElevenLabs")
		return
	}
	if upstreamStatus != http.StatusOK {
		log.Println("elevenlabs returned status", upstreamStatus)
		writeJSONError(w, http.StatusBadGateway, "ElevenLabs returned an error (status "+strconv.Itoa(upstreamStatus)+")")
		return
	}

	w.Header().Set("Content-Type", "audio/mpeg")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(audio)
}

// fetchSpeech calls the ElevenLabs text-to-speech endpoint and returns the
// raw MP3 bytes plus ElevenLabs' own HTTP status code.
func fetchSpeech(apiKey, voiceID, text string) ([]byte, int, error) {
	payload, err := json.Marshal(elevenLabsRequestBody{
		Text:    text,
		ModelID: modelID,
		VoiceSettings: map[string]float64{
			"stability":        0.5,
			"similarity_boost": 0.75,
		},
	})
	if err != nil {
		return nil, 0, err
	}

	url := fmt.Sprintf("%s/v1/text-to-speech/%s", elevenLabsBaseURL(), voiceID)
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("xi-api-key", apiKey)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "audio/mpeg")

	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, 0, err
	}
	return data, resp.StatusCode, nil
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status":             "ok",
		"elevenlabs_key_set": os.Getenv("ELEVENLABS_API_KEY") != "",
	})
}

// loadDotEnv is a tiny, dependency-free stand-in for python-dotenv's
// load_dotenv(): reads KEY=VALUE lines from .env into the process
// environment, without overriding variables that are already set.
func loadDotEnv(path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return // no .env file present — fine, rely on real env vars
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, found := strings.Cut(line, "=")
		if !found {
			continue
		}
		key = strings.TrimSpace(key)
		if _, alreadySet := os.LookupEnv(key); !alreadySet {
			os.Setenv(key, strings.TrimSpace(value))
		}
	}
}

func main() {
	loadDotEnv(".env")

	port := os.Getenv("PORT")
	if port == "" {
		port = "8081"
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/tts", ttsHandler)
	mux.HandleFunc("/health", healthHandler)

	// A bare http.ListenAndServe has no read/write/idle timeouts, so a
	// slow or malicious client can hold a connection (and its goroutine)
	// open indefinitely. Configuring them explicitly is standard practice
	// for any Go HTTP server, not just one meant for real traffic.
	server := &http.Server{
		Addr:         ":" + port,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second, // generous: covers ElevenLabs' own response time
		IdleTimeout:  60 * time.Second,
	}

	log.Println("Meduza voice-service listening on :" + port)
	if os.Getenv("ELEVENLABS_API_KEY") == "" {
		log.Println("warning: ELEVENLABS_API_KEY is not set — /tts will return 503 until it is")
	}
	if err := server.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
