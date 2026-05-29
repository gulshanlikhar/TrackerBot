import json
import os
import boto3

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "openai.gpt-oss-20b-1:0")

client = boto3.client("bedrock-runtime", region_name=AWS_REGION)


def llm_json(prompt: str, fallback):
    try:
        response = client.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": (
                                "You are GovTrack AI. Return only valid JSON. "
                                "No markdown, no explanation.\n\n"
                                + prompt
                            )
                        }
                    ],
                }
            ],
            inferenceConfig={
                "maxTokens": 2048,
                "temperature": 0.1,
                "topP": 0.9,
            },
        )

        content_blocks = response["output"]["message"]["content"]
        text = "\n".join(
            block["text"] for block in content_blocks if "text" in block
        ).strip()

        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)

    except Exception as e:
        print(f"Bedrock Converse failed: {e}")
        return fallback