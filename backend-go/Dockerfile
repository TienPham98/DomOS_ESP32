# --- Build stage -----------------------------------------------------------
FROM golang:1.25-bookworm AS builder

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /out/domos-server ./cmd/server

# --- Runtime stage -----------------------------------------------------------
FROM alpine:3.20

RUN apk add --no-cache ca-certificates tzdata

WORKDIR /app
COPY --from=builder /out/domos-server ./domos-server

RUN mkdir -p /app/uploads/wallpapers /app/uploads/firmware

EXPOSE 8080
ENTRYPOINT ["./domos-server"]
