output "bus_name" {
  value = aws_cloudwatch_event_bus.main.name
}

output "bus_arn" {
  value = aws_cloudwatch_event_bus.main.arn
}
