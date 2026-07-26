// Package otelx initialises OpenTelemetry tracing and metrics.
//
// Services export over OTLP to a central collector rather than talking to
// Prometheus, Loki and Tempo directly. That indirection is deliberate: the
// storage backends can be swapped without redeploying a single service, which is
// the Maintainability tactic the architecture document commits to.
//
// When no endpoint is configured the whole thing degrades to no-op providers, so
// running a service locally never requires a collector.
package otelx

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
)

// Config configures the telemetry pipeline.
type Config struct {
	// Enabled turns telemetry on. When false, no-op providers are installed.
	Enabled bool
	// OTLPEndpoint is the collector's gRPC address, e.g. "otel-collector:4317".
	OTLPEndpoint string
	// Insecure disables TLS to the collector, which is normal inside a cluster.
	Insecure bool
	// ServiceName, ServiceVersion and Environment become resource attributes.
	ServiceName    string
	ServiceVersion string
	Environment    string
	// SampleRatio is the head-sampling probability. 1.0 traces everything, which
	// is right for a course demo and for financial flows; lower it for volume.
	SampleRatio float64
	// MetricInterval is how often metrics are pushed to the collector.
	MetricInterval time.Duration
	// ExportTimeout bounds a single export.
	ExportTimeout time.Duration
}

// Providers holds what was installed, so that the caller can shut it down.
type Providers struct {
	tracer   trace.Tracer
	shutdown []func(context.Context) error
	logger   *slog.Logger
}

// Tracer returns a tracer for manual instrumentation.
func (p *Providers) Tracer() trace.Tracer { return p.tracer }

// Shutdown flushes and stops every provider. It must be called before exit or
// the last batch of spans is lost.
func (p *Providers) Shutdown(ctx context.Context) error {
	var errs []error
	// Shut down in reverse order of installation.
	for i := len(p.shutdown) - 1; i >= 0; i-- {
		if err := p.shutdown[i](ctx); err != nil {
			errs = append(errs, err)
		}
	}
	if len(errs) > 0 {
		return fmt.Errorf("otelx: shutdown: %w", errors.Join(errs...))
	}
	return nil
}

// Setup installs global tracer and meter providers.
//
// The W3C trace-context propagator is installed unconditionally, including in the
// disabled case, so that a trace id created upstream still flows through this
// service's logs even when this service is not exporting spans of its own.
func Setup(ctx context.Context, cfg Config, logger *slog.Logger) (*Providers, error) {
	if logger == nil {
		logger = slog.Default()
	}

	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	providers := &Providers{logger: logger}

	if !cfg.Enabled || cfg.OTLPEndpoint == "" {
		logger.Info("telemetry export is disabled; using no-op providers")
		providers.tracer = otel.Tracer(cfg.ServiceName)
		return providers, nil
	}

	if cfg.SampleRatio <= 0 {
		cfg.SampleRatio = 1.0
	}
	if cfg.MetricInterval <= 0 {
		cfg.MetricInterval = 15 * time.Second
	}
	if cfg.ExportTimeout <= 0 {
		cfg.ExportTimeout = 10 * time.Second
	}

	res, err := resource.Merge(resource.Default(), resource.NewWithAttributes(
		semconv.SchemaURL,
		semconv.ServiceName(cfg.ServiceName),
		semconv.ServiceVersion(cfg.ServiceVersion),
		semconv.DeploymentEnvironment(cfg.Environment),
		attribute.String("platform", "arcadia"),
	))
	if err != nil {
		return nil, fmt.Errorf("otelx: build resource: %w", err)
	}

	traceOpts := []otlptracegrpc.Option{
		otlptracegrpc.WithEndpoint(cfg.OTLPEndpoint),
		otlptracegrpc.WithTimeout(cfg.ExportTimeout),
	}
	if cfg.Insecure {
		traceOpts = append(traceOpts, otlptracegrpc.WithInsecure())
	}
	traceExporter, err := otlptracegrpc.New(ctx, traceOpts...)
	if err != nil {
		return nil, fmt.Errorf("otelx: create trace exporter: %w", err)
	}

	tracerProvider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExporter,
			sdktrace.WithBatchTimeout(5*time.Second),
			sdktrace.WithMaxExportBatchSize(512),
		),
		sdktrace.WithResource(res),
		// ParentBased keeps a trace whole: once the gateway decides to sample a
		// request, every downstream service honours that decision instead of
		// re-rolling the dice and producing a trace with holes in it.
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(cfg.SampleRatio))),
	)
	otel.SetTracerProvider(tracerProvider)
	providers.shutdown = append(providers.shutdown, tracerProvider.Shutdown)

	metricOpts := []otlpmetricgrpc.Option{
		otlpmetricgrpc.WithEndpoint(cfg.OTLPEndpoint),
		otlpmetricgrpc.WithTimeout(cfg.ExportTimeout),
	}
	if cfg.Insecure {
		metricOpts = append(metricOpts, otlpmetricgrpc.WithInsecure())
	}
	metricExporter, err := otlpmetricgrpc.New(ctx, metricOpts...)
	if err != nil {
		// Traces already work; losing metrics is not worth failing the boot over,
		// but it must be loud.
		logger.Error("failed to create the metric exporter; continuing without OTLP metrics",
			slog.String("error", err.Error()))
	} else {
		meterProvider := sdkmetric.NewMeterProvider(
			sdkmetric.WithResource(res),
			sdkmetric.WithReader(sdkmetric.NewPeriodicReader(metricExporter,
				sdkmetric.WithInterval(cfg.MetricInterval),
			)),
		)
		otel.SetMeterProvider(meterProvider)
		providers.shutdown = append(providers.shutdown, meterProvider.Shutdown)
	}

	providers.tracer = otel.Tracer(cfg.ServiceName)
	logger.Info("telemetry initialised",
		slog.String("endpoint", cfg.OTLPEndpoint),
		slog.Float64("sample_ratio", cfg.SampleRatio),
	)
	return providers, nil
}

// StartSpan is a small convenience over the global tracer.
func StartSpan(ctx context.Context, name string, attrs ...attribute.KeyValue) (context.Context, trace.Span) {
	return otel.Tracer("arcadia").Start(ctx, name, trace.WithAttributes(attrs...))
}

// RecordError marks the active span as failed. Callers use it at the boundary
// where an error becomes a response, so that a trace shows which span broke.
func RecordError(span trace.Span, err error) {
	if err == nil || span == nil {
		return
	}
	span.RecordError(err)
	span.SetStatus(otelcodes.Error, err.Error())
}
